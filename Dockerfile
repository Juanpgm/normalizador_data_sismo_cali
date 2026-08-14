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
COPY job.py run_integration.py ./

# Logs land on the mounted volume; the job degrades to stdout-only if absent.
ENV LOG_DIR=/data/logs

CMD ["python", "job.py"]
