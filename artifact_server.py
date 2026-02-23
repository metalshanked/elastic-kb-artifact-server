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
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import APIRouter, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("ARTIFACT_DATA_DIR", "/data"))
HOST = os.environ.get("ARTIFACT_HOST", "0.0.0.0")
PORT = int(os.environ.get("ARTIFACT_PORT", "8080"))
MAX_CONTENT_MB = int(os.environ.get("ARTIFACT_MAX_UPLOAD_MB", "500"))

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

# ---------------------------------------------------------------------------
# HTML Template (single-page UI)
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  .btn-secondary {{ background: #6c757d; color: #fff; }}
  .btn-secondary:hover {{ background: #565e64; }}
  .btn-success {{ background: #28a745; color: #fff; }}
  .btn-success:hover {{ background: #218838; }}
  .btn:disabled {{ opacity: .6; cursor: not-allowed; }}
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
  /* Download from Elastic styles */
  .dl-controls {{ display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; margin-top: .75rem; }}
  .dl-controls select, .dl-controls label {{ font-size: .95rem; }}
  .dl-controls select {{ padding: .4rem .6rem; border: 1px solid var(--border); border-radius: 6px; min-width: 120px; }}
  .dl-controls label {{ display: flex; align-items: center; gap: .35rem; cursor: pointer; }}
  #dl-progress {{ margin-top: .75rem; display: none; }}
  #dl-progress .progress-bar {{ height: 22px; background: #e9ecef; border-radius: 6px; overflow: hidden; margin: .5rem 0; }}
  #dl-progress .progress-fill {{ height: 100%; background: var(--accent); transition: width .3s; display: flex; align-items: center; justify-content: center; color: #fff; font-size: .8rem; font-weight: 600; }}
  #dl-log {{ max-height: 200px; overflow-y: auto; font-family: 'Consolas', 'Courier New', monospace; font-size: .82rem; background: #1a1a2e; color: #d4d4d4; padding: .75rem; border-radius: 6px; margin-top: .5rem; white-space: pre-wrap; word-break: break-all; }}
  #dl-log .log-ok {{ color: #4ec9b0; }}
  #dl-log .log-err {{ color: #f48771; }}
  #dl-log .log-info {{ color: #9cdcfe; }}
</style>
</head>
<body>
<div class="container">
  <h1>🗄️ Elastic KB Artifact Server</h1>
  <p class="subtitle">Upload &amp; serve Kibana AI Assistant knowledge base artifacts (S3-compliant)</p>

  {flash_html}

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

  <!-- Download from Elastic -->
  <div class="card">
    <h2>🌐 Download from Elastic</h2>
    <p style="margin-bottom:.75rem;color:var(--muted)">
      Fetch official knowledge base artifacts from the Elastic S3 bucket directly through your browser,
      then automatically upload them to this server. Your browser's proxy settings are used for the download.
    </p>
    <div>
      <button id="dl-fetch-btn" class="btn btn-secondary" onclick="fetchElasticIndex()">Fetch Available Versions</button>
    </div>
    <div class="dl-controls" id="dl-controls" style="display:none">
      <select id="dl-version"><option value="">— select version —</option></select>
      <label><input type="checkbox" id="dl-multilingual"> Include multilingual</label>
      <button id="dl-start-btn" class="btn btn-success" onclick="startDownloadUpload()">Download &amp; Upload</button>
    </div>
    <div id="dl-progress">
      <div class="progress-bar"><div class="progress-fill" id="dl-progress-fill" style="width:0%"></div></div>
      <div id="dl-log"></div>
    </div>
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
const ELASTIC_BASE = "https://kibana-knowledge-base-artifacts.elastic.co";
const PRODUCTS = ["elasticsearch", "kibana", "observability", "security"];
const SUBPATH = "{subpath}";
let allArtifacts = [];

function humanSize(bytes) {{
  for (const u of ["B", "KB", "MB", "GB"]) {{
    if (bytes < 1024) return bytes.toFixed(1) + " " + u;
    bytes /= 1024;
  }}
  return bytes.toFixed(1) + " TB";
}}

function logMsg(text, cls) {{
  const el = document.getElementById("dl-log");
  const span = document.createElement("span");
  if (cls) span.className = cls;
  span.textContent = text + "\\n";
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}}

function setProgress(pct) {{
  const fill = document.getElementById("dl-progress-fill");
  fill.style.width = pct + "%";
  fill.textContent = Math.round(pct) + "%";
}}

async function fetchElasticIndex() {{
  const btn = document.getElementById("dl-fetch-btn");
  btn.disabled = true;
  btn.textContent = "Fetching…";
  document.getElementById("dl-progress").style.display = "block";
  document.getElementById("dl-log").innerHTML = "";
  logMsg("Fetching artifact index from " + ELASTIC_BASE + " …", "log-info");

  try {{
    const resp = await fetch(ELASTIC_BASE);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const xmlText = await resp.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(xmlText, "application/xml");

    allArtifacts = [];
    const contents = doc.getElementsByTagNameNS("http://doc.s3.amazonaws.com/2006-03-01", "Contents");
    /* Fallback: try without namespace */
    const items = contents.length > 0 ? contents : doc.getElementsByTagName("Contents");
    for (const c of items) {{
      const keyEl = c.getElementsByTagNameNS("http://doc.s3.amazonaws.com/2006-03-01", "Key")[0]
                 || c.getElementsByTagName("Key")[0];
      const sizeEl = c.getElementsByTagNameNS("http://doc.s3.amazonaws.com/2006-03-01", "Size")[0]
                  || c.getElementsByTagName("Size")[0];
      const modEl = c.getElementsByTagNameNS("http://doc.s3.amazonaws.com/2006-03-01", "LastModified")[0]
                 || c.getElementsByTagName("LastModified")[0];
      if (keyEl) {{
        allArtifacts.push({{
          key: keyEl.textContent,
          size: parseInt(sizeEl ? sizeEl.textContent : "0", 10),
          lastModified: modEl ? modEl.textContent : ""
        }});
      }}
    }}
    logMsg("Found " + allArtifacts.length + " artifacts in the index.", "log-ok");

    /* Extract unique versions */
    const versions = new Set();
    for (const a of allArtifacts) {{
      if (!a.key.startsWith("kb-product-doc-")) continue;
      for (const prod of PRODUCTS) {{
        const prefix = "kb-product-doc-" + prod + "-";
        if (a.key.startsWith(prefix)) {{
          const rest = a.key.slice(prefix.length);
          const ver = rest.split(".zip")[0].split("--")[0];
          if (ver && /^\\d/.test(ver)) versions.add(ver.replace(/\\.$/, ""));
        }}
      }}
    }}
    const sorted = Array.from(versions).sort((a, b) => {{
      const pa = a.split(".").map(Number), pb = b.split(".").map(Number);
      for (let i = 0; i < Math.max(pa.length, pb.length); i++) {{
        if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0);
      }}
      return 0;
    }});
    const sel = document.getElementById("dl-version");
    sel.innerHTML = '<option value="">— select version —</option>';
    for (const v of sorted) {{
      const opt = document.createElement("option");
      opt.value = v; opt.textContent = v;
      sel.appendChild(opt);
    }}
    logMsg("Available versions: " + sorted.join(", "), "log-info");
    document.getElementById("dl-controls").style.display = "flex";
  }} catch (err) {{
    logMsg("ERROR: " + err.message, "log-err");
    logMsg("Your browser may be blocking the cross-origin request. Check the console for CORS errors.", "log-err");
  }} finally {{
    btn.disabled = false;
    btn.textContent = "Fetch Available Versions";
  }}
}}

function filterArtifacts(version, multilingual) {{
  const matched = [];
  for (const a of allArtifacts) {{
    if (a.key.indexOf("-" + version + ".") < 0 && !a.key.endsWith("-" + version + ".zip")) continue;
    const isMulti = a.key.indexOf("--.") >= 0 || a.key.indexOf("multilingual") >= 0;
    if (isMulti && !multilingual) continue;
    matched.push(a);
  }}
  return matched;
}}

async function startDownloadUpload() {{
  const version = document.getElementById("dl-version").value;
  if (!version) {{ alert("Please select a version."); return; }}
  const multilingual = document.getElementById("dl-multilingual").checked;
  const matched = filterArtifacts(version, multilingual);
  if (matched.length === 0) {{
    logMsg("No artifacts found for version " + version + ".", "log-err");
    return;
  }}

  const btn = document.getElementById("dl-start-btn");
  const fetchBtn = document.getElementById("dl-fetch-btn");
  btn.disabled = true; fetchBtn.disabled = true;
  btn.textContent = "Working…";
  document.getElementById("dl-progress").style.display = "block";
  setProgress(0);

  const totalSize = matched.reduce((s, a) => s + a.size, 0);
  logMsg("Downloading " + matched.length + " artifact(s) (" + humanSize(totalSize) + ") for version " + version + " …", "log-info");

  let ok = 0, fail = 0;
  for (let i = 0; i < matched.length; i++) {{
    const a = matched[i];
    const url = ELASTIC_BASE + "/" + a.key;
    logMsg("[" + (i + 1) + "/" + matched.length + "] Downloading " + a.key + " (" + humanSize(a.size) + ") …", "log-info");
    try {{
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const blob = await resp.blob();
      logMsg("  ✓ Downloaded. Uploading to server …", "log-ok");

      /* Upload to our server */
      const fd = new FormData();
      fd.append("files", blob, a.key);
      const upResp = await fetch(SUBPATH + "/upload", {{ method: "POST", body: fd, redirect: "manual" }});
      if (upResp.status === 303 || upResp.status === 200 || upResp.ok || upResp.type === "opaqueredirect") {{
        logMsg("  ✓ Uploaded " + a.key + " to server.", "log-ok");
        ok++;
      }} else {{
        throw new Error("Upload HTTP " + upResp.status);
      }}
    }} catch (err) {{
      logMsg("  ✗ FAILED: " + err.message, "log-err");
      fail++;
    }}
    setProgress(((i + 1) / matched.length) * 100);
  }}

  logMsg("", "");
  logMsg("Done — " + ok + " uploaded, " + fail + " failed.", ok > 0 ? "log-ok" : "log-err");
  btn.disabled = false; fetchBtn.disabled = false;
  btn.textContent = "Download & Upload";
  if (ok > 0) {{
    logMsg("Reloading page in 2 seconds …", "log-info");
    setTimeout(() => window.location.href = SUBPATH + "/", 2000);
  }}
}}
</script>
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

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, msg: str = "", mtype: str = "success"):
    return HTMLResponse(_render_html(request, flash_msg=msg, flash_type=mtype))


@router.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...)):
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
async def delete(version: str, filename: str):
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
    print(f"Listening on  : http://{HOST}:{PORT}{SUBPATH}/")
    print(f"Upload UI     : http://localhost:{PORT}{SUBPATH}/")
    print()
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
    )
