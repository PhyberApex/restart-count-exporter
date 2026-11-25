FROM python:3.15-slim@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5

WORKDIR /app
COPY exporter.py .

EXPOSE 9103
CMD ["python", "/app/exporter.py"]
