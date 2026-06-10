FROM python:3.12-slim

WORKDIR /app
COPY bmo-web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bmo-web/bmo_web ./bmo_web
EXPOSE 8000

CMD ["python", "-m", "bmo_web.app"]

