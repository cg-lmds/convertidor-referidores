from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.processor import (
    _sanitize_filename,
    build_all_zip_bytes,
    build_excel_bytes,
    filter_records,
    parse_and_merge,
    summarize_by_referidor,
)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

SESSION_TTL_SECONDS = 3600  # 60 minutes auto-expiration

app = FastAPI(title="Segmentador de Referidores — XtraCash / BOVEM", version="1.0.0")

_lock = threading.Lock()
_current_dataset: dict[str, Any] | None = None
_last_updated_ts: float = 0.0


def _get_active_dataset() -> dict[str, Any]:
    global _current_dataset, _last_updated_ts
    with _lock:
        if _current_dataset is None:
            raise HTTPException(
                status_code=404,
                detail="No hay datos cargados actualmente. Sube DATA ORIGEN y REFERIDORES para comenzar.",
            )
        if time.time() - _last_updated_ts > SESSION_TTL_SECONDS:
            _current_dataset = None
            _last_updated_ts = 0.0
            raise HTTPException(
                status_code=410,
                detail="La sesión temporal expiró por inactividad. Vuelve a cargar los archivos.",
            )
        _last_updated_ts = time.time()
        return _current_dataset


def _parse_vencimientos_param(vencimientos: str | None) -> list[str] | None:
    if not vencimientos:
        return None
    parts = [p.strip() for p in vencimientos.split(",") if p.strip()]
    return parts if parts else None


def _recalc_if_needed(dataset: dict[str, Any], tasa: float | None) -> tuple[list[dict[str, Any]], float]:
    active_tasa = dataset["tasa"]
    if tasa is not None and tasa > 0 and abs(tasa - active_tasa) > 1e-6:
        # Update dataset in-place with new tasa
        with _lock:
            for rec in dataset["records"]:
                rec["pago_minimo_usd"] = round(rec["pago_minimo_bs"] / tasa, 2)
                rec["pago_total_usd"] = round(rec["pago_total_bs"] / tasa, 2)
            dataset["tasa"] = tasa
        active_tasa = tasa
    return dataset["records"], active_tasa


def _content_disposition(filename: str) -> str:
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "archivo.xlsx"
    encoded = quote(filename)
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/bcv-rate")
def get_bcv_rate() -> JSONResponse:
    """Fetch official BCV USD/Bs rate from public API."""
    endpoints = [
        ("https://ve.dolarapi.com/v1/dolares/oficial", lambda d: (d.get("promedio"), d.get("fechaActualizacion"))),
        ("https://pydolarve.org/api/v1/dollar?page=bcv", lambda d: (d.get("monitors", {}).get("usd", {}).get("price"), d.get("datetime", {}).get("date"))),
    ]
    for url, extractor in endpoints:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                rate, updated = extractor(data)
                if rate and float(rate) > 0:
                    return JSONResponse(
                        {
                            "ok": True,
                            "rate": round(float(rate), 4),
                            "updated_at": updated or "",
                            "source": "BCV Oficial",
                        }
                    )
        except Exception:
            continue

    return JSONResponse(
        {
            "ok": False,
            "message": "No se pudo consultar la tasa BCV automáticamente. Ingrésala manualmente.",
        },
        status_code=200,
    )


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    global _current_dataset, _last_updated_ts
    with _lock:
        if _current_dataset is None:
            return {"has_data": False}
        if time.time() - _last_updated_ts > SESSION_TTL_SECONDS:
            _current_dataset = None
            _last_updated_ts = 0.0
            return {"has_data": False}
        return {
            "has_data": True,
            "tasa": _current_dataset["tasa"],
            "created_at": _current_dataset["created_at"],
            "stats": _current_dataset["stats"],
            "ttl_minutes": int(SESSION_TTL_SECONDS / 60),
        }


@app.post("/api/process")
async def process_files(
    data_origen: UploadFile = File(...),
    referidores: UploadFile = File(...),
    tasa: float = Form(...),
) -> dict[str, Any]:
    global _current_dataset, _last_updated_ts

    if tasa <= 0:
        raise HTTPException(status_code=400, detail="La tasa del día debe ser mayor a 0.")

    origen_bytes = await data_origen.read()
    ref_bytes = await referidores.read()

    if not origen_bytes or not ref_bytes:
        raise HTTPException(status_code=400, detail="Ambos archivos Excel son obligatorios.")

    try:
        dataset = parse_and_merge(origen_bytes, ref_bytes, tasa)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Error al procesar los archivos Excel: {exc}",
        ) from exc

    # Replace previous temporary dataset in memory
    with _lock:
        _current_dataset = dataset
        _last_updated_ts = time.time()

    summary = summarize_by_referidor(dataset["records"])
    total_pmin = round(sum(r["pago_minimo_usd"] for r in dataset["records"]), 2)
    total_deuda = round(sum(r["pago_total_usd"] for r in dataset["records"]), 2)

    return {
        "ok": True,
        "tasa": dataset["tasa"],
        "created_at": dataset["created_at"],
        "stats": dataset["stats"],
        "kpis": {
            "total_clientes": len(dataset["records"]),
            "total_referidores": len(summary),
            "pago_minimo_usd": total_pmin,
            "pago_total_usd": total_deuda,
        },
        "referidores": summary,
    }


@app.get("/api/summary")
def get_summary(
    only_with_debt: bool = Query(False),
    only_overdue: bool = Query(False),
    vencimientos: str | None = Query(None),
    include_sin_referidor: bool = Query(True),
    tasa: float | None = Query(None),
) -> dict[str, Any]:
    dataset = _get_active_dataset()
    records, active_tasa = _recalc_if_needed(dataset, tasa)

    filtered = filter_records(
        records,
        only_with_debt=only_with_debt,
        only_overdue=only_overdue,
        vencimientos=_parse_vencimientos_param(vencimientos),
        include_sin_referidor=include_sin_referidor,
    )
    summary = summarize_by_referidor(filtered)
    total_pmin = round(sum(r["pago_minimo_usd"] for r in filtered), 2)
    total_deuda = round(sum(r["pago_total_usd"] for r in filtered), 2)

    return {
        "ok": True,
        "tasa": active_tasa,
        "created_at": dataset["created_at"],
        "stats": dataset["stats"],
        "kpis": {
            "total_clientes": len(filtered),
            "total_referidores": len(summary),
            "pago_minimo_usd": total_pmin,
            "pago_total_usd": total_deuda,
        },
        "referidores": summary,
    }


@app.get("/api/referidor-detail")
def get_referidor_detail(
    referidor: str = Query(...),
    only_with_debt: bool = Query(False),
    only_overdue: bool = Query(False),
    vencimientos: str | None = Query(None),
    include_sin_referidor: bool = Query(True),
) -> dict[str, Any]:
    dataset = _get_active_dataset()
    filtered = filter_records(
        dataset["records"],
        only_with_debt=only_with_debt,
        only_overdue=only_overdue,
        vencimientos=_parse_vencimientos_param(vencimientos),
        include_sin_referidor=include_sin_referidor,
    )
    rows = [r for r in filtered if r["referidor"] == referidor]
    rows.sort(key=lambda r: (-r["pago_minimo_usd"], r["nombre"].lower()))

    return {
        "ok": True,
        "referidor": referidor,
        "total": len(rows),
        "pago_minimo_usd": round(sum(r["pago_minimo_usd"] for r in rows), 2),
        "pago_total_usd": round(sum(r["pago_total_usd"] for r in rows), 2),
        "rows": rows,
    }


@app.get("/api/download/referidor")
def download_referidor_excel(
    referidor: str = Query(...),
    only_with_debt: bool = Query(False),
    only_overdue: bool = Query(False),
    vencimientos: str | None = Query(None),
    include_sin_referidor: bool = Query(True),
    include_pago_total: bool = Query(True),
) -> Response:
    dataset = _get_active_dataset()
    filtered = filter_records(
        dataset["records"],
        only_with_debt=only_with_debt,
        only_overdue=only_overdue,
        vencimientos=_parse_vencimientos_param(vencimientos),
        include_sin_referidor=include_sin_referidor,
    )
    rows = [r for r in filtered if r["referidor"] == referidor]
    if not rows:
        raise HTTPException(status_code=404, detail="No hay registros para este referidor con los filtros actuales.")

    xlsx_bytes = build_excel_bytes(rows, include_pago_total=include_pago_total, sheet_title="Cartera")
    safe_name = _sanitize_filename(referidor)
    date_tag = datetime.now().strftime("%Y-%m-%d")
    filename = f"{safe_name}_{date_tag}.xlsx"

    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@app.get("/api/download/zip")
def download_all_zip(
    only_with_debt: bool = Query(False),
    only_overdue: bool = Query(False),
    vencimientos: str | None = Query(None),
    include_sin_referidor: bool = Query(True),
    include_pago_total: bool = Query(True),
) -> Response:
    dataset = _get_active_dataset()
    filtered = filter_records(
        dataset["records"],
        only_with_debt=only_with_debt,
        only_overdue=only_overdue,
        vencimientos=_parse_vencimientos_param(vencimientos),
        include_sin_referidor=include_sin_referidor,
    )
    if not filtered:
        raise HTTPException(status_code=404, detail="No hay registros para exportar con los filtros actuales.")

    zip_bytes = build_all_zip_bytes(filtered, include_pago_total=include_pago_total)
    date_tag = datetime.now().strftime("%Y-%m-%d")
    filename = f"Referidores_Segmentados_{date_tag}.zip"

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@app.get("/api/download/consolidado")
def download_consolidado(
    only_with_debt: bool = Query(False),
    only_overdue: bool = Query(False),
    vencimientos: str | None = Query(None),
    include_sin_referidor: bool = Query(True),
    include_pago_total: bool = Query(True),
) -> Response:
    dataset = _get_active_dataset()
    filtered = filter_records(
        dataset["records"],
        only_with_debt=only_with_debt,
        only_overdue=only_overdue,
        vencimientos=_parse_vencimientos_param(vencimientos),
        include_sin_referidor=include_sin_referidor,
    )
    if not filtered:
        raise HTTPException(status_code=404, detail="No hay registros para exportar con los filtros actuales.")

    xlsx_bytes = build_excel_bytes(filtered, include_pago_total=include_pago_total, sheet_title="Consolidado")
    date_tag = datetime.now().strftime("%Y-%m-%d")
    filename = f"Consolidado_Referidores_{date_tag}.xlsx"

    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@app.post("/api/clear")
def clear_session() -> dict[str, Any]:
    global _current_dataset, _last_updated_ts
    with _lock:
        _current_dataset = None
        _last_updated_ts = 0.0
    return {"ok": True, "message": "Datos temporales eliminados de memoria."}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
