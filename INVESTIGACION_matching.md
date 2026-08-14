# Investigación: maximizar el match de forma SEGURA

Objetivo: subir la tasa de emparejamiento EDAN ↔ Visitas por encima del 62%
**sin introducir falsos positivos** (dato diligenciado a mano por voluntarios).

## TL;DR

El **62.1% (653/1051) es prácticamente el techo seguro** para estos datos. Se
investigaron y **midieron** métodos de análisis de direcciones, ML, DL y modelos
matemáticos; el margen seguro adicional es de **~0–2 pares**. La única mejora
segura aplicada (normalización de ordinales) no sube el conteo pero mueve ~6
matches al nivel más confiable (`handshake`). El límite no es el algoritmo: es
que **las dos fuentes no se solapan más** y la **grilla de Cali** genera
direcciones casi idénticas que NO son el mismo sitio.

## Evidencia del techo (4 mediciones independientes)

| Método de sondeo | Recuperables marcados | Verdaderos tras verificación |
|---|---|---|
| Consenso multi-señal laxo (tfidf+JW+ts+emb+geo) | 22 | ~5 (el resto, vecinos de cuadra) |
| Triple numérico exacto + tipo vía + barrio | 9 | ~5 (resto, ruido del parser) |
| Huella exacta sobre normalización agresiva | 1 | 1 |
| Vector exacto sobre normalización agresiva | 1 | 1 |
| **Re-ranker ML (HistGradientBoosting + negativos duros)** | **0** | **0** |

Falsos positivos típicos que la grilla produce (por qué NO se puede aflojar):
`CL 8 # 38-120` vs `CL 8 # 39-120` · `CL 11 # 9-20` vs `CL 13 # 9-20` ·
`KR 69 # 1-70` vs `KR 70 # 1C-70` — texto casi idéntico, **sitios distintos**.

## Métodos evaluados (ranking del research + resultado empírico)

1. **Record linkage probabilístico (Fellegi-Sunter / Splink)** — el research lo
   pone #1 por el ajuste por frecuencia (baja el peso de números comunes). No se
   integró: requiere dependencia pesada y el techo medido (~0–2) no lo justifica.
   Su beneficio (recombinar señales) se probó vía el re-ranker ML → 0 ganancia.
2. **Blocking (barrio / grilla de coords)** — aplicado en el experimento ML.
   Seguro y sustractivo; habilita umbrales más laxos dentro del bloque, pero no
   destrabó pares nuevos porque los candidatos intra-barrio son justamente los
   vecinos de cuadra ambiguos.
3. **Métricas de string extra (Jaro-Winkler, Damerau-Levenshtein, Jaccard)** —
   `rapidfuzz`, ya instalado. Usadas como features del re-ranker. Útiles pero no
   suficientes para separar verdaderos de vecinos de grilla.
4. **Clasificador supervisado sobre etiquetas plata (HistGradientBoosting)** —
   entrenado con 608 positivos exactos + 6.881 **negativos duros** (vecinos de
   cuadra del mismo barrio). CV a p≥0.97: **precisión 0.933, recall 0.488**.
   Ni al umbral alto alcanza precisión segura → **+0 matches** con guard estricto.
   Es la prueba central del techo. Script: `integracion/experiments/ml_reranker.py`.
5. **Embeddings transformer (MiniLM) vs model2vec** — el research lo estima de
   bajo impacto para direcciones numéricas; los números son semánticamente
   cercanos ("3"≈"5") y solo ayudarían en nombres de edificio. No se agregó torch
   por ganancia marginal; ya hay un nivel `embedding` (model2vec).
6. **Crosswalk de nomenclatura colombiana (ordinales, orientación, bis)** —
   **APLICADO**: `4TA→4`, `1RA→1`, etc. en `canonicalize_for_match`. `bis` y
   sufijos de letra se mantienen como discriminadores (evita `26`≡`26A`). Efecto:
   +6 al nivel `handshake` (mayor confianza), conteo total igual.
7. **libpostal / deepparse (parsing estadístico/DL)** — **descartado**: sin ruta
   limpia de instalación en Windows/Python 3.14 y sin entrenamiento en Colombia;
   el normalizador IGAC propio ya supera a un parser multinacional genérico.

## Recomendación

- **No aflojar** umbrales fuzzy ni aceptar por una sola señal: la grilla castiga.
- La mejora de recall real vendrá de **más solapamiento de datos**, no de modelos:
  (a) el **puente espacial catastral** cuando el servidor de Cali vuelva
  (`--fetch-parcels`), que empareja por predio aunque el texto difiera; y
  (b) completar coordenadas en origen (hoy solo ~9% de EDAN las tiene).
- Mantener el re-ranker ML como monitor: si a futuro entran datos con más
  solapamiento, su precisión subirá y podrá aportar de forma segura.
