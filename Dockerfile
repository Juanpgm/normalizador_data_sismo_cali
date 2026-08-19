FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the static embedding model into the image. Downloading it on every
# scheduled execution would add minutes and a network dependency to a job that
# otherwise finishes in seconds.
RUN python -c "from model2vec import StaticModel; StaticModel.from_pretrained('minishlab/potion-multilingual-128M')"

COPY integracion/ ./integracion/
COPY basemaps/ ./basemaps/
COPY job.py run_integration.py \
     integrar_f3.py asignar_f3.py job_integrar_f3.py job_asignaciones.py \
     cruce_criticos_survey.py subir_cruce_firebase.py job_cruce.py ./

# Logs land on the mounted volume; the job degrades to stdout-only if absent.
ENV LOG_DIR=/data/logs

# One image, three cron services: each Railway service overrides the start
# command (job.py hourly · job_integrar_f3.py every 2h · job_asignaciones.py
# daily 16:00 Bogota). See scripts/railway_setup.py.
CMD ["python", "job.py"]
