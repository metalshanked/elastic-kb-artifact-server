FROM python:3.12-slim

LABEL maintainer="elastica"
LABEL description="Elastic KB Artifact Server — upload & serve Kibana AI Assistant knowledge base artifacts"

WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.115.* uvicorn[standard]==0.34.* python-multipart==0.0.*

COPY artifact_server.py .

ENV ARTIFACT_DATA_DIR=/data
ENV ARTIFACT_HOST=0.0.0.0
ENV ARTIFACT_PORT=8080
ENV ARTIFACT_MAX_UPLOAD_MB=500
ENV ARTIFACT_SUBPATH=""

EXPOSE 8080

VOLUME ["/data"]

CMD ["python", "artifact_server.py"]
