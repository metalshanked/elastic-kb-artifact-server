# Elastic KB Artifact Server

A lightweight, self-hosted artifact server for **Kibana AI Assistant knowledge base** files. Upload, manage, and serve `kb-product-doc-*.zip` artifacts with an S3-compliant XML bucket listing — so you can point Kibana at your own infrastructure instead of the public Elastic bucket.

---

## Features

| Capability | Description |
|---|---|
| **Browser UI** | Single-page web interface to upload, list, and delete artifact zip files |
| **S3-compatible listing** | Per-version XML bucket listing that Kibana understands out of the box |
| **Auto version detection** | Extracts the version from the filename pattern `kb-product-doc-<product>-<major>.<minor>.zip` |
| **Overwrite protection** | Re-uploading the same artifact for a version replaces the previous file |
| **Subpath support** | Host behind a reverse proxy at any subpath via `ARTIFACT_SUBPATH` |
| **Docker ready** | Minimal `python:3.12-slim` image with a persistent `/data` volume |
| **Downloader script** | CLI utility to fetch official artifacts from the Elastic S3 bucket and optionally mirror them locally |

## Quick Start

### Run with Docker (recommended)

```bash
docker build -t elastic-kb-artifact-server .
docker run -d \
  --name elastic-kb-artifact-server \
  -p 8080:8080 \
  -v elastic-artifacts:/data \
  elastic-kb-artifact-server
```

Open **http://localhost:8080** in your browser to access the upload UI.

### Run directly with Python

```bash
pip install fastapi uvicorn[standard] python-multipart
python artifact_server.py
```

The server starts on `http://0.0.0.0:8080` by default.

## Configuration

All settings are controlled via environment variables:

| Variable | Default | Description |
|---|---|---|
| `ARTIFACT_DATA_DIR` | `/data` | Directory where uploaded artifacts are stored |
| `ARTIFACT_HOST` | `0.0.0.0` | Host address to bind the server to |
| `ARTIFACT_PORT` | `8080` | Port number for the server |
| `ARTIFACT_MAX_UPLOAD_MB` | `500` | Maximum upload file size in megabytes |
| `ARTIFACT_SUBPATH` | *(empty)* | URL subpath prefix (e.g. `/kibana-artifacts`) |

### Subpath example

```bash
ARTIFACT_SUBPATH=/kibana-artifacts python artifact_server.py
# UI at         http://localhost:8080/kibana-artifacts/
# Kibana config http://<host>:8080/kibana-artifacts/artifacts/9.3
```

## Kibana Integration

Point Kibana at your self-hosted server by adding the following to `kibana.yml`:

```yaml
xpack.productDocBase.artifactRepositoryUrl: "http://<host>:<port>/artifacts/<version>"
```

For example, if you host version **9.3** artifacts:

```yaml
xpack.productDocBase.artifactRepositoryUrl: "http://myserver:8080/artifacts/9.3"
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Browser UI — upload & manage artifacts |
| `POST` | `/upload` | Upload one or more `.zip` artifact files |
| `DELETE` | `/delete/{version}/{filename}` | Delete a specific artifact |
| `GET` | `/artifacts/{version}/` | S3-compliant XML bucket listing for a version |
| `GET` | `/artifacts/{version}/{filename}` | Download a specific artifact file |

> All paths are relative to `ARTIFACT_SUBPATH` when configured.

## Download Official Elastic Artifacts

The included `download_elastic_artifacts.py` script fetches official knowledge base artifacts from the **Elastic S3 bucket** — useful for mirroring or seeding your server.

```bash
# Interactive version prompt
python download_elastic_artifacts.py

# Download all 9.3 artifacts
python download_elastic_artifacts.py --version 9.3

# Include multilingual variants
python download_elastic_artifacts.py --version 9.3 --multilingual

# List all available artifacts
python download_elastic_artifacts.py --list

# Custom output directory
python download_elastic_artifacts.py --output ./my-folder
```

### Proxy support

```bash
# Via flag
python download_elastic_artifacts.py --proxy http://proxy.corp:8080 --version 9.3

# Via environment variable
HTTP_PROXY=http://proxy:8080 python download_elastic_artifacts.py --version 9.3
```

## Project Structure

```
elastic-kb-artifact-server/
├── artifact_server.py              # FastAPI server with browser UI
├── download_elastic_artifacts.py   # CLI downloader for official Elastic artifacts
├── Dockerfile                      # Production-ready container image
├── LICENSE                         # MIT License
└── README.md                       # This file
```

## Requirements

- **Python 3.12+**
- [FastAPI](https://fastapi.tiangolo.com/) `0.115.*`
- [Uvicorn](https://www.uvicorn.org/) `0.34.*`
- [python-multipart](https://pypi.org/project/python-multipart/) `0.0.*`

The downloader script (`download_elastic_artifacts.py`) uses **only the Python standard library** — no extra dependencies required.

## License

This project is licensed under the [MIT License](LICENSE).
