ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app
COPY bmo-web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bmo-web/bmo_web ./bmo_web
COPY bmo-web/plugins ./plugins
EXPOSE 8000

CMD ["python", "-m", "bmo_web.app"]
