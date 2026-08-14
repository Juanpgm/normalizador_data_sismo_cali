# Normalizador de datos — sismo Cali

Integra dos fuentes de la emergencia en una sola tabla normalizada y la publica
cada hora en Google Sheets:

- **EDAN** — evaluación de daños (hoja `EDAN 100826 - Datos Madre`)
- **Visitas** — reportes de campo (respuestas de formulario)

Las dos fuentes se diligencian a mano, por personas distintas, y **no comparten
llave**. El paquete `integracion/` las cruza con una cascada de ocho niveles de
evidencia (handshake de dirección, vector, puente espacial, TF-IDF, fuzzy,
embeddings) y descarta todo par por debajo de un umbral de confianza.

El detalle del pipeline está en [`README_integracion.md`](README_integracion.md);
el análisis de por qué el 62% de match es el techo seguro, en
[`INVESTIGACION_matching.md`](INVESTIGACION_matching.md).

## Qué se publica

El job escribe **únicamente dos hojas** del documento `EDAN SISMO`:

| Hoja | Contenido |
|---|---|
| `tabla_integrada` | La tabla completa: **todos** los registros, con match y sin match. La columna `fuente` distingue `edan+visita`, `solo_edan` y `solo_visita`. |
| `integracion_stats` | Todas las estadísticas de la corrida: matches por método, tasa, cobertura de coordenadas, distribución de confianza, todos los umbrales usados y la procedencia de la ejecución. |

Ninguna otra hoja se toca. El publicador resuelve las hojas destino por título
**y** verifica su `sheetId` contra el valor fijado en `integracion/config.py`: si
alguien renombra o recrea una pestaña, la corrida aborta en vez de escribir en el
lugar equivocado. Nunca crea, borra ni duplica hojas.

## Uso local

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate en Linux/macOS
pip install -r requirements.txt
```

Poné el key del service account en `service_account.json` en la raíz (está en
`.gitignore`; **nunca** se commitea) o exportá `GOOGLE_SERVICE_ACCOUNT_JSON` con
su contenido.

```bash
python run_integration.py                                  # corrida normal, Excel en output/
python run_integration.py --fresh                          # ignora el cache, relee las hojas
python run_integration.py --no-embedding                   # sin el nivel LM (más rápido)
python run_integration.py --fresh --no-export --to-sheets  # lo que corre el job por hora
pytest -q
```

`--fresh` no es opcional para publicar: sin ese flag el pipeline lee los pickles
de `output/cache/` y republicaría datos viejos.

## Automatización

`.github/workflows/hourly.yml` corre el pipeline cada hora (`cron: "0 * * * *"`)
y publica el resultado. La credencial vive en el secret
`GOOGLE_SERVICE_ACCOUNT_JSON` del repositorio:

```bash
gh secret set GOOGLE_SERVICE_ACCOUNT_JSON --repo Juanpgm/normalizador_data_sismo_cali < service_account.json
```

Dos cosas para tener presentes del scheduler de GitHub: el cron es *best-effort*
y puede demorarse algunos minutos, y GitHub **desactiva los workflows agendados
tras 60 días sin actividad en el repositorio** — si el proyecto queda quieto, hay
que reactivarlo desde la pestaña Actions.

## Datos personales

Las fuentes contienen datos de personas afectadas. Este repositorio es público,
así que:

- `service_account.json`, `output/`, `data/` y todo `*.xlsx` están en `.gitignore`;
- los notebooks se commitean **sin outputs** (`python scripts/strip_notebooks.py`);
  CI lo verifica en cada push;
- la tabla publicada no incluye nombres, correos ni teléfonos.
