# Elastic KB Artifact Server

A lightweight, self-hosted artifact server for **Kibana AI Assistant knowledge base** files. Upload, manage, and serve `kb-product-doc-*.zip` artifacts with an S3-compliant XML bucket listing — so you can point Kibana at your own infrastructure instead of the public Elastic bucket.

---

## Features

| Capability | Description |
|---|---|
| **Browser UI** | Single-page web interface to upload, list, and delete artifact zip files |
| **S3-compatible listing** | Per-version XML bucket listing that Kibana understands out of the box |
| **UI Link Generator** | Generate direct download links to official Elastic S3 artifacts right in the browser (bypassing CORS safely) |
| **Scalable** | Production-ready multi-worker support via Uvicorn to handle concurrent traffic |
| **Auto version detection** | Extracts the version from the filename pattern `kb-product-doc-<product>-<major>.<minor>.zip` |
| **Overwrite protection** | Re-uploading the same artifact for a version replaces the previous file |
| **Subpath support** | Host behind a reverse proxy at any subpath via `ARTIFACT_SUBPATH` |
| **Docker ready** | Minimal `python:3.12-slim` image with a persistent `/data` volume |

## Quick Start

### Run with Docker (recommended)

```bash
docker build -t elastic-kb-artifact-server .
docker run -d \
  --name elastic-kb-artifact-server \
  -p 8080:8080 \
  -v elastic-artifacts:/data \
  elastic-kb-artifact-server