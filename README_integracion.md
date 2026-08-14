# Integración EDAN ↔ Visitas — Emergencia sismo Cali

Módulos Python que integran las dos fuentes (`df_edan` y `df_visitas`) en una
sola tabla, **sin una llave común**, resolviendo la identidad de cada sitio por
una cascada de niveles ordenada por confianza. Reproduce fielmente la lógica del
notebook `EDA.ipynb` (no lo modifica) y agrega: **puente espacial catastral**,
**métricas** y **tests de progreso**.

> Los datos son diligenciados a mano por voluntarios: pueden no coincidir con el
> catastro oficial. Por eso ningún nivel confía ciegamente en una sola señal;
> todos llevan *guards* (barrio + coherencia numérica) y las coordenadas se usan
> como corroboración, nunca como verdad absoluta.

## Uso

```bash
pip install -r requirements.txt
python run_integration.py            # corrida completa (usa caché si existe)
python run_integration.py --fresh    # re-lee Google Sheets
python run_integration.py --no-embedding   # sin nivel LM (más rápido)
pytest -q                            # tests + tests de progreso
```

Entregable: `output/integracion_consolidada.xlsx` con 4 hojas:
`consolidada` (tabla magra con `sitio_id`, `visita_id`, `trust_score`),
`confiable` (solo `trust_score > 0.7`), `matches` (pares con método y trust) y
`metricas`. Además `output/metricas.json`.

## Cascada de matching (primer acierto gana)

| # | Método | Idea | Guard |
|---|--------|------|-------|
| 1 | `handshake` | huella canónica exacta | — |
| 2 | `vector` | distancia 3D ≤ 0.05 (sub-cuadra) | — |
| 3 | `vector_block` | misma vía+cruce, placa ≤ 40 | barrio |
| 4 | `corner` | esquina independiente del orden | barrio |
| 5 | `geo` | haversine ≤ 40 m (90 m si EDAN sin dirección) | coherencia laxa |
| — | **`spatial_bridge`** | **mismo predio catastral / cúmulo espacial** | barrio + coherencia + tope de distancia |
| 6 | `tfidf` | TF-IDF char n-gramas + coseno ≥ 0.82 | coherencia + barrio |
| 7 | `fuzzy` | `token_set_ratio` ≥ 88 | coherencia + barrio + largo mín. |
| 8 | `embedding` | LM model2vec, coseno ≥ 0.75 | guard numérico reforzado + barrio |

Corte de confiabilidad: solo sobreviven matches con `trust ≥ 0.70`.

## Puente espacial (`integracion/spatial_bridge.py`)

Dos registros cuyas coordenadas caen en el **mismo polígono de predio** son el
mismo sitio, aunque sus direcciones estén escritas distinto. Dos modos:

- **`parcel`** — hay una capa catastral en `data/catastro/*.geojson` (WGS84).
  Se hace point-in-polygon (shapely STRtree) y se emparejan los registros que
  comparten `id` de predio, con tope de distancia + guards.
- **`surrogate`** — sin capa disponible: enlace por proximidad (≤ 22 m) con los
  mismos guards. Se descuenta el `trust` por la incertidumbre de coordenadas a
  mano.

Para activar el modo `parcel`: dejá un GeoJSON de lotes/predios de Cali en
`data/catastro/`. El campo de id se detecta automáticamente (NPN, CBML,
CODIGO_PREDIAL, LOTE_CODIGO, …).

## Estructura

```
integracion/
  config.py         umbrales, rutas, IDs de hojas
  io_sheets.py      lectura + limpieza + IDs reutilizados (fuente de verdad)
  normalization.py  normalización IGAC + huella + vector 3D
  coords.py         parseo a WGS84 (DMS, decimal, MAGNA-SIRGAS EPSG:3115)
  matching.py       canonicalización, guards y cascada (niveles 1-7)
  spatial_bridge.py puente espacial (parcel / surrogate)
  embedding.py      nivel semántico LM (model2vec)
  trust.py          score de confiabilidad + corte
  integrate.py      merge externo + tabla integrada + subconjuntos
  metrics.py        métricas y reporte de progreso
  pipeline.py       orquestación end-to-end + export Excel
tests/              normalización, coords, guards, puente espacial, progreso
run_integration.py  CLI
```
