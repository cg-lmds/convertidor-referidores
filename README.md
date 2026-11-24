# Segmentador de Referidores — XtraCash / BOVEM

Aplicación web diseñada para desplegarse en **Render** que cruza automáticamente `DATA ORIGEN.xlsx` con `REFERIDORES.xlsx`, calcula los montos en **USD** según la tasa del día y genera reportes Excel individuales por referidor (o un archivo `.ZIP` con todos los referidores comprimidos).

## Características principales

- **Cruce automático por documento de identidad:** Empareja `Doc. Ident.` (DATA ORIGEN) contra `Nac + Cedula` (REFERIDORES) para asignar el `Referidor`.
- **Conversión a USD con tasa del día:** Permite ingresar la tasa manualmente o consultarla en un clic desde la tasa BCV oficial.
- **Formato estándar corporativo:** Genera cada Excel con estilos (`#2B4C7E`, formato moneda `$#,##0.00`, teléfonos como texto, encabezados congelados y autofiltro) y las columnas:
  - `Documento de Identidad`
  - `Nombre del Cliente`
  - `Teléfono`
  - `Email`
  - `Pago Mínimo Pendiente (USD)`
  - `Pago Total Pendiente (USD)` *(opcional, activable/desactivable desde la interfaz)*
  - `Tipo de Vencimiento`
  - `Referidor`
- **DataTable interactivo:** Búsqueda en tiempo real por referidor, ordenamiento por columnas, filtros por vencimiento y saldo pendiente, vista previa en modal y botón de **Descargar Excel** junto a cada referidor.
- **Descarga masiva en `.ZIP` y Consolidado `.XLSX`:** Genera los archivos al vuelo en memoria sin acumular archivos en disco.
- **Datos 100% temporales:** Toda la información vive en memoria RAM, se reemplaza automáticamente al cargar un nuevo corte, expira tras 60 minutos de inactividad y puede limpiarse manualmente con el botón **Limpiar datos**.

## Ejecución local

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Luego abre `http://localhost:8000` en tu navegador.

## Despliegue en Render

1. Sube este proyecto a un repositorio de GitHub/GitLab (el archivo `.gitignore` ya excluye automáticamente todos los `.xlsx` sensibles).
2. En [Render](https://render.com), selecciona **New → Blueprint** y conecta tu repositorio (detectará `render.yaml` automáticamente), o crea un **Web Service** con:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path:** `/health`
