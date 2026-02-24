#!/usr/bin/env python3
"""
Elastic KB Artifact Server — single-page web app with upload UI.

Serves Kibana AI Assistant knowledge base artifacts with an S3-compliant XML
bucket listing, organized by version.  Upload zip files via the browser UI and
point Kibana at:

    xpack.productDocBase.artifactRepositoryUrl: "http://<host>:<port>/artifacts/<version>"

Features:
    - Browser UI to upload / manage artifact zip files
    - Auto-detects version from filename (kb-product-doc-*-<major>.<minor>.zip)
    - Same-version artifacts overwrite previous uploads
    - Per-version S3-compliant XML bucket listing at /artifacts/<version>/
    - Serves zip files at /artifacts/<version>/<filename>
    - Lists all hosted versions on the main page
    - Configurable subpath via ARTIFACT_SUBPATH env var (e.g. /kibana-artifacts)
    - Scalable multi-worker support via ARTIFACT_WORKERS env var

Run directly:
    pip install fastapi uvicorn python-multipart
    python artifact_server.py

Run via Docker:
    docker build -t elastic-artifact-server .
    docker run -p 8080:8080 -v elastic-artifacts:/data elastic-artifact-server

Subpath example:
    ARTIFACT_SUBPATH=/kibana-artifacts python artifact_server.py
    # UI at http://localhost:8080/kibana-artifacts/
    # Kibana config: http://<host>:8080/kibana-artifacts/artifacts/9.3
"""

import os
import re
import platform
import hmac
import hashlib
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("ARTIFACT_DATA_DIR", "/data"))
HOST = os.environ.get("ARTIFACT_HOST", "0.0.0.0")
PORT = int(os.environ.get("ARTIFACT_PORT", "8080"))
MAX_CONTENT_MB = int(os.environ.get("ARTIFACT_MAX_UPLOAD_MB", "500"))
WORKERS = int(os.environ.get("ARTIFACT_WORKERS", "1"))
UI_USERNAME = os.environ.get("UI_USERNAME", "").strip()
UI_PASSWORD = os.environ.get("UI_PASSWORD", "").strip()
UI_AUTH_ENABLED = bool(UI_USERNAME and UI_PASSWORD)
UI_COOKIE_NAME = "ui_session"
_raw_ui_session_secret = os.environ.get("UI_SESSION_SECRET", "").strip()
UI_SESSION_SECRET = _raw_ui_session_secret or f"{UI_USERNAME}:{UI_PASSWORD}"

# Subpath support — strip/add leading/trailing slashes for consistency
_raw_subpath = os.environ.get("ARTIFACT_SUBPATH", "").strip("/")
SUBPATH = f"/{_raw_subpath}" if _raw_subpath else ""

PRODUCTS = ["elasticsearch", "kibana", "observability", "security"]
FILENAME_RE = re.compile(
    r"^kb-product-doc-(?P<product>[a-z]+)-(?P<version>\d+\.\d+)(?P<suffix>[^.]*)\.zip$"
)

app = FastAPI(
    title="Elastic KB Artifact Server",
    docs_url=None,
    redoc_url=None,
)
router = APIRouter(prefix=SUBPATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _secure_filename(filename: str) -> str:
    """Sanitise a filename — keep only safe characters."""
    filename = filename.replace("\\", "/")
    filename = filename.split("/")[-1]
    # Keep alphanumeric, dashes, underscores, dots
    filename = re.sub(r"[^\w\-.]", "_", filename)
    return filename.strip("._") or "unnamed"


def get_versions() -> list[str]:
    """Return sorted list of version directories that contain at least one zip."""
    if not DATA_DIR.exists():
        return []
    versions = []
    for d in sorted(DATA_DIR.iterdir()):
        if d.is_dir() and any(d.glob("*.zip")):
            versions.append(d.name)
    try:
        versions.sort(key=lambda v: list(map(int, v.split("."))))
    except ValueError:
        versions.sort()
    return versions


def get_artifacts(version: str) -> list[dict]:
    """Return metadata dicts for every zip in a version folder."""
    version_dir = DATA_DIR / version
    if not version_dir.is_dir():
        return []
    artifacts = []
    for f in sorted(version_dir.glob("*.zip")):
        stat = f.stat()
        artifacts.append({
            "key": f.name,
            "size": stat.st_size,
            "last_modified": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        })
    return artifacts


def build_s3_xml(artifacts: list[dict]) -> str:
    """Generate an S3-compliant ListBucketResult XML string."""
    contents = ""
    for a in artifacts:
        contents += f"""    <Contents>
        <Key>{a['key']}</Key>
        <LastModified>{a['last_modified']}</LastModified>
        <Size>{a['size']}</Size>
    </Contents>
"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
    <Name>kibana-ai-assistant-kb-artifacts</Name>
    <Prefix/>
    <Marker/>
    <IsTruncated>false</IsTruncated>
{contents}</ListBucketResult>
"""


def parse_filename(filename: str) -> dict | None:
    """Extract product and version from an artifact filename. Returns None on mismatch."""
    m = FILENAME_RE.match(filename)
    if m:
        return m.groupdict()
    return None


def human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _base_url(request: Request) -> str:
    """Build the external-facing base URL, including the configured subpath."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    return f"{scheme}://{host}{SUBPATH}"


def _make_session_cookie_value(username: str) -> str:
    payload = urlsafe_b64encode(username.encode("utf-8")).decode("ascii")
    sig = hmac.new(
        UI_SESSION_SECRET.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


def _is_valid_session_cookie(cookie_value: str | None) -> bool:
    if not UI_AUTH_ENABLED:
        return True
    if not cookie_value or "." not in cookie_value:
        return False

    payload, sig = cookie_value.rsplit(".", 1)
    expected = hmac.new(
        UI_SESSION_SECRET.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        username = urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
    except Exception:
        return False
    return username == UI_USERNAME


def _require_ui_login(request: Request):
    if not UI_AUTH_ENABLED:
        return None
    if _is_valid_session_cookie(request.cookies.get(UI_COOKIE_NAME)):
        return None

    next_path = request.url.path
    if request.url.query:
        next_path += f"?{request.url.query}"
    safe_next = _safe_next_path(next_path)
    return RedirectResponse(url=f"{SUBPATH}/login?next={quote(safe_next, safe='')}", status_code=303)


def _safe_next_path(next_path: str) -> str:
    if next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return f"{SUBPATH}/"


# ---------------------------------------------------------------------------
# HTML Template (single-page UI)
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Elastic KB Artifact Server</title>
<style>
  :root {{ --bg: #f5f7fa; --card: #fff; --accent: #0077cc; --border: #dde1e6; --text: #1a1a2e; --muted: #6b7280; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; padding: 2rem; }}
  .container {{ max-width: 960px; margin: 0 auto; }}
  h1 {{ font-size: 1.8rem; margin-bottom: .5rem; }}
  h2 {{ font-size: 1.3rem; margin: 1.5rem 0 .75rem; border-bottom: 2px solid var(--accent); padding-bottom: .25rem; }}
  .subtitle {{ color: var(--muted); margin-bottom: 1.5rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1.25rem; }}
  .upload-form {{ display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; }}
  .upload-form input[type=file] {{ flex: 1 1 300px; }}
  .btn {{ display: inline-block; padding: .5rem 1.25rem; border: none; border-radius: 6px; cursor: pointer; font-size: .95rem; font-weight: 500; text-decoration: none; }}
  .btn-primary {{ background: var(--accent); color: #fff; }}
  .btn-primary:hover {{ background: #005fa3; }}
  .btn-danger {{ background: #dc3545; color: #fff; font-size: .8rem; padding: .3rem .75rem; }}
  .btn-danger:hover {{ background: #b02a37; }}
  .btn-outline {{ background: transparent; border: 1px solid var(--border); color: var(--text); padding: .4rem .75rem; font-size: .85rem; }}
  .btn-outline:hover {{ background: #f0f2f5; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: .5rem; }}
  th, td {{ padding: .5rem .75rem; text-align: left; border-bottom: 1px solid var(--border); }}
  th {{ background: #f0f2f5; font-weight: 600; font-size: .85rem; text-transform: uppercase; letter-spacing: .03em; }}
  td {{ font-size: .9rem; }}
  .mono {{ font-family: 'Consolas', 'Courier New', monospace; font-size: .85rem; background: #eef1f5; padding: .15rem .4rem; border-radius: 4px; }}
  .tag {{ display: inline-block; background: #e3f2fd; color: #0d47a1; padding: .15rem .5rem; border-radius: 4px; font-size: .8rem; font-weight: 600; margin-right: .25rem; }}
  .flash {{ padding: .75rem 1rem; border-radius: 6px; margin-bottom: 1rem; }}
  .flash-success {{ background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
  .flash-error {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .empty {{ text-align: center; color: var(--muted); padding: 2rem; }}
  footer {{ margin-top: 2rem; text-align: center; color: var(--muted); font-size: .8rem; }}
</style>
</head>
<body>
<div class="container">
  <h1>🗄️ Elastic KB Artifact Server</h1>
  <p class="subtitle">Upload &amp; serve Kibana AI Assistant knowledge base artifacts (S3-compliant)</p>

  {flash_html}

  <!-- Generate Download Links -->
  <div class="card" style="background: #f8fafd; border-color: #cce0ff;">
    <h2>📥 Get Artifacts from Elastic</h2>
    <p style="margin-bottom:.75rem;color:var(--muted); font-size:.9rem;">
      Need artifacts? Enter a version below to generate direct secure download links from Elastic. 
      Click the buttons to download them to your computer, then upload them via the form below.
    </p>
    <div style="display:flex; flex-wrap:wrap; gap:.75rem; align-items:center; margin-bottom: 1rem;">
      <input type="text" id="dl-version" placeholder="e.g. 9.3" style="padding:.5rem; border:1px solid var(--border); border-radius:4px; width:120px;">
      <label style="font-size:.9rem; cursor:pointer;"><input type="checkbox" id="dl-multi"> Include multilingual</label>
    </div>
    <div id="dl-links" style="display:flex; flex-wrap:wrap; gap:.5rem;">
      <span style="color:var(--muted); font-size:.85rem; font-style:italic;">Enter a version above to show links...</span>
    </div>
  </div>

  <!-- Upload -->
  <div class="card">
    <h2>📤 Upload Artifacts</h2>
    <p style="margin-bottom:.75rem;color:var(--muted)">
      Upload <code>kb-product-doc-&lt;product&gt;-&lt;major&gt;.&lt;minor&gt;[...].zip</code> files.
      Version is auto-detected from the filename. Same-version files are overwritten.
    </p>
    <form class="upload-form" method="POST" action="{subpath}/upload" enctype="multipart/form-data">
      <input type="file" name="files" accept=".zip" multiple required>
      <button type="submit" class="btn btn-primary">Upload</button>
    </form>
  </div>

  <!-- Versions -->
  <div class="card">
    <h2>📦 Hosted Versions</h2>
    {versions_html}
  </div>

  <!-- Per-version detail -->
  {details_html}

  <footer>
    Platform: {platform_info} &middot; Data dir: <code>{data_dir}</code>
    {subpath_info}
  </footer>
</div>

<script>
  // Link Generator Script (Runs completely in browser, ignores CORS)
  const ELASTIC_BASE = "https://kibana-knowledge-base-artifacts.elastic.co";
  const PRODUCTS = ["elasticsearch", "kibana", "observability", "security"];

  const vInput = document.getElementById("dl-version");
  const mInput = document.getElementById("dl-multi");
  const linkContainer = document.getElementById("dl-links");

  function updateLinks() {{
    const v = vInput.value.trim();
    if (!v || !/^\\d+\\.\\d+/.test(v)) {{
      linkContainer.innerHTML = '<span style="color:var(--muted); font-size:.85rem; font-style:italic;">Enter a valid version (e.g., 9.3) to show links...</span>';
      return;
    }}

    linkContainer.innerHTML = "";
    const multi = mInput.checked;

    PRODUCTS.forEach(p => {{
      createLink(`kb-product-doc-${{p}}-${{v}}.zip`);
      if (multi) {{
        createLink(`kb-product-doc-${{p}}-${{v}}--.zip`);
      }}
    }});
  }}

  function createLink(filename) {{
    const a = document.createElement("a");
    a.href = `${{ELASTIC_BASE}}/${{filename}}`;
    a.className = "btn btn-outline";
    a.setAttribute("download", filename); // Hints to browser to download instead of navigate
    a.target = "_blank"; // Fallback to open in new tab if download hint fails
    a.innerHTML = `⬇️ ${{filename}}`;
    linkContainer.appendChild(a);
  }}

  vInput.addEventListener("input", updateLinks);
  mInput.addEventListener("change", updateLinks);
</script>
</body>
</html>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Login - Elastic KB Artifact Server</title>
<style>
  :root {{ --bg: #f5f7fa; --card: #fff; --accent: #0077cc; --border: #dde1e6; --text: #1a1a2e; --muted: #6b7280; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; padding: 2rem; }}
  .wrap {{ min-height: calc(100vh - 4rem); display: flex; align-items: center; justify-content: center; }}
  .card {{ width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.5rem; }}
  h1 {{ font-size: 1.4rem; margin-bottom: .5rem; }}
  .subtitle {{ color: var(--muted); margin-bottom: 1rem; }}
  .field {{ margin-bottom: .9rem; }}
  label {{ display: block; font-size: .9rem; margin-bottom: .2rem; }}
  input {{ width: 100%; padding: .55rem .65rem; border: 1px solid var(--border); border-radius: 6px; font-size: .95rem; }}
  .btn {{ width: 100%; padding: .55rem .65rem; border: none; border-radius: 6px; cursor: pointer; font-size: .95rem; font-weight: 500; background: var(--accent); color: #fff; }}
  .btn:hover {{ background: #005fa3; }}
  .error {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; border-radius: 6px; padding: .55rem .65rem; margin-bottom: .9rem; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>UI Login</h1>
    <p class="subtitle">Authentication is required for UI access and uploads.</p>
    {error_html}
    <form method="POST" action="{subpath}/login">
      <input type="hidden" name="next" value="{next_path}">
      <div class="field">
        <label for="username">Username</label>
        <input id="username" name="username" type="text" autocomplete="username" required>
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
      </div>
      <button type="submit" class="btn">Sign In</button>
    </form>
  </div>
</div>
</body>
</html>
"""


def _render_html(request: Request, flash_msg: str = "", flash_type: str = "success") -> str:
    """Render the single-page HTML UI."""
    base = _base_url(request)
    versions = get_versions()

    # Flash message
    flash_html = ""
    if flash_msg:
        cls = "flash-success" if flash_type == "success" else "flash-error"
        flash_html = f'<div class="flash {cls}">{flash_msg}</div>'

    # Versions table
    if versions:
        rows = ""
        for v in versions:
            rows += (
                f"<tr>"
                f'<td><span class="tag">{v}</span></td>'
                f'<td><a href="{SUBPATH}/artifacts/{v}/">Browse (XML)</a></td>'
                f'<td><span class="mono">{base}/artifacts/{v}</span></td>'
                f"</tr>"
            )
        versions_html = (
            "<table><thead><tr><th>Version</th><th>Artifacts</th>"
            "<th>Kibana Config URL</th></tr></thead><tbody>"
            f"{rows}</tbody></table>"
        )
    else:
        versions_html = '<div class="empty">No artifacts uploaded yet. Use the form above to get started.</div>'

    # Per-version detail cards
    details_html = ""
    for v in versions:
        arts = get_artifacts(v)
        art_rows = ""
        for a in arts:
            art_rows += (
                f"<tr>"
                f'<td><a href="{SUBPATH}/artifacts/{v}/{a["key"]}">{a["key"]}</a></td>'
                f"<td>{human_size(a['size'])}</td>"
                f"<td>{a['last_modified']}</td>"
                f"<td>"
                f'<form method="POST" action="{SUBPATH}/delete/{v}/{a["key"]}" style="display:inline"'
                f' onsubmit="return confirm(\'Delete {a["key"]}?\')">'
                f'<button type="submit" class="btn btn-danger">Delete</button></form>'
                f"</td></tr>"
            )
        details_html += (
            f'<div class="card"><h2>Version {v}</h2>'
            f"<table><thead><tr><th>File</th><th>Size</th><th>Modified</th><th></th></tr></thead>"
            f"<tbody>{art_rows}</tbody></table></div>"
        )

    subpath_info = f"&middot; Subpath: <code>{SUBPATH}</code>" if SUBPATH else ""

    return HTML_TEMPLATE.format(
        flash_html=flash_html,
        subpath=SUBPATH,
        versions_html=versions_html,
        details_html=details_html,
        platform_info=platform.platform(),
        data_dir=str(DATA_DIR.resolve()),
        subpath_info=subpath_info,
    )


def _render_login_html(next_path: str = "", error_msg: str = "") -> str:
    safe_next = _safe_next_path(next_path)
    error_html = f'<div class="error">{error_msg}</div>' if error_msg else ""
    return LOGIN_TEMPLATE.format(
        subpath=SUBPATH,
        next_path=safe_next,
        error_html=error_html,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, msg: str = "", mtype: str = "success"):
    redirect = _require_ui_login(request)
    if redirect:
        return redirect
    return HTMLResponse(_render_html(request, flash_msg=msg, flash_type=mtype))


@router.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...)):
    redirect = _require_ui_login(request)
    if redirect:
        return redirect
    uploaded = []
    errors = []

    for f in files:
        fname = _secure_filename(f.filename or "")
        info = parse_filename(fname)
        if not info:
            errors.append(f"{fname}: filename does not match expected pattern")
            continue

        # Enforce max upload size
        content = await f.read()
        if len(content) > MAX_CONTENT_MB * 1024 * 1024:
            errors.append(f"{fname}: exceeds {MAX_CONTENT_MB} MB limit")
            continue

        version = info["version"]
        version_dir = DATA_DIR / version
        version_dir.mkdir(parents=True, exist_ok=True)

        dest = version_dir / fname
        dest.write_bytes(content)
        uploaded.append(f"{fname} → v{version}")

    parts = []
    if uploaded:
        parts.append(f"Uploaded: {', '.join(uploaded)}")
    if errors:
        parts.append(f"Errors: {'; '.join(errors)}")

    mtype = "success" if uploaded and not errors else ("error" if errors and not uploaded else "success")
    msg = " | ".join(parts)
    return RedirectResponse(url=f"{SUBPATH}/?msg={msg}&mtype={mtype}", status_code=303)


@router.post("/delete/{version}/{filename}")
async def delete(request: Request, version: str, filename: str):
    redirect = _require_ui_login(request)
    if redirect:
        return redirect
    safe_name = _secure_filename(filename)
    target = DATA_DIR / version / safe_name
    if target.exists():
        target.unlink()
        # Remove empty version directory
        version_dir = DATA_DIR / version
        if version_dir.is_dir() and not any(version_dir.glob("*.zip")):
            version_dir.rmdir()
        msg = f"Deleted {safe_name} from v{version}."
        return RedirectResponse(url=f"{SUBPATH}/?msg={msg}&mtype=success", status_code=303)
    return RedirectResponse(url=f"{SUBPATH}/?msg=File not found: {safe_name}&mtype=error", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = ""):
    if not UI_AUTH_ENABLED:
        return RedirectResponse(url=f"{SUBPATH}/", status_code=303)
    if _is_valid_session_cookie(request.cookies.get(UI_COOKIE_NAME)):
        destination = _safe_next_path(next)
        return RedirectResponse(url=destination, status_code=303)
    return HTMLResponse(_render_login_html(next_path=next))


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
):
    if not UI_AUTH_ENABLED:
        return RedirectResponse(url=f"{SUBPATH}/", status_code=303)
    if not secrets.compare_digest(username, UI_USERNAME) or not secrets.compare_digest(password, UI_PASSWORD):
        return HTMLResponse(
            _render_login_html(next_path=next, error_msg="Invalid username or password."),
            status_code=401,
        )

    destination = _safe_next_path(next)
    response = RedirectResponse(url=destination, status_code=303)
    response.set_cookie(
        key=UI_COOKIE_NAME,
        value=_make_session_cookie_value(username),
        httponly=True,
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        samesite="lax",
        path=(SUBPATH or "/"),
    )
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse(url=f"{SUBPATH}/login", status_code=303)
    response.delete_cookie(key=UI_COOKIE_NAME, path=(SUBPATH or "/"))
    return response


@router.get("/artifacts/{version}")
@router.get("/artifacts/{version}/")
async def artifact_index(version: str):
    """S3-compliant XML bucket listing for a specific version."""
    artifacts = get_artifacts(version)
    xml = build_s3_xml(artifacts)
    return Response(content=xml, media_type="application/xml")


@router.get("/artifacts/{version}/{filename}")
async def artifact_file(version: str, filename: str):
    """Serve an individual artifact zip file."""
    version_dir = DATA_DIR / version
    safe_name = _secure_filename(filename)
    file_path = version_dir / safe_name
    if not file_path.is_file():
        return Response(content="File not found", status_code=404)
    return FileResponse(
        path=str(file_path.resolve()),
        filename=safe_name,
        media_type="application/zip",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
app.include_router(router)

if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Platform      : {platform.system()} ({platform.platform()})")
    print(f"Data directory: {DATA_DIR.resolve()}")
    print(f"Subpath       : {SUBPATH or '(none)'}")
    print(f"Workers       : {WORKERS}")
    print(f"UI login      : {'enabled' if UI_AUTH_ENABLED else 'disabled'}")
    print(f"Listening on  : http://{HOST}:{PORT}{SUBPATH}/")
    print(f"Upload UI     : http://{HOST}:{PORT}{SUBPATH}/")
    print()

    # Run using the import string "artifact_server:app" to enable multiple workers
    uvicorn.run(
        "artifact_server:app",
        host=HOST,
        port=PORT,
        workers=WORKERS,
    )
