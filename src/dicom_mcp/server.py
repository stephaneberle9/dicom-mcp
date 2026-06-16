"""
DICOM MCP Server main implementation.
"""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Dict, List, Any, AsyncIterator

from fastmcp import FastMCP, Context
from fastmcp.apps import AppConfig, ResourceCSP
from fastmcp.tools.tool import ToolResult

from .attributes import ATTRIBUTE_PRESETS
from .dicom_client import DicomClient
from .config import DicomConfiguration, load_config


# Configure logging
logger = logging.getLogger("dicom_mcp")


@dataclass
class DicomContext:
    """Context for the DICOM MCP server."""
    config: DicomConfiguration
    client: DicomClient


# PDF viewer widget: renders an Encapsulated-PDF report inline via pdf.js (canvas).
# pdf.js + the ext-apps runtime load from CDNs, which the iframe CSP must allow-list.
# If rendering fails (CSP/worker blocked, malformed PDF), the widget degrades to the
# server-extracted text fallback so the report is still readable.
PDF_VIEW_WIDGET_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8">
<style>
  :root { color-scheme: light dark; }
  body { font: 14px system-ui, sans-serif; margin: 0; padding: 12px;
         background: #f7f7f8; color: #111; }
  html.dark body { background: #1e1e20; color: #eee; }
  .bar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .bar h1 { font-size: 14px; margin: 0; flex: 1; }
  button { font: 13px system-ui; padding: 4px 10px; border-radius: 6px;
           border: 1px solid #ccc; background: #fff; cursor: pointer; }
  html.dark button { background: #333; color: #eee; border-color: #555; }
  #pages canvas { width: 100%; height: auto; display: block; margin: 0 auto 10px;
                  border: 1px solid #ddd; border-radius: 6px; background: #fff; }
  #status { color: #666; font-size: 13px; }
  html.dark #status { color: #aaa; }
  #fallback { white-space: pre-wrap; font: 13px ui-monospace, monospace;
              background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 10px; }
  html.dark #fallback { background: #2a2a2d; border-color: #444; }
  .hidden { display: none; }
</style></head>
<body>
  <div class="bar">
    <h1>📄 DICOM Report (Encapsulated PDF)</h1>
  </div>
  <div id="status">Loading PDF &hellip;</div>
  <div id="pages"></div>
  <pre id="fallback" class="hidden"></pre>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"
        integrity="sha512-q+4liFwdPC/bNdhUpZx6aXDx/h77yEQtn4I1slHydcbZK34nLaR3cAeYSJshoxIOq3mjEf7xJE8YWIUHMn+oCQ=="
        crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script type="module">
import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";

function readToolData(result) {
  if (result && result.structuredContent && typeof result.structuredContent === "object")
    return result.structuredContent;
  const t = (result?.content || []).find(c => c.type === "text");
  try { return JSON.parse(t?.text || "{}"); } catch (e) { return {}; }
}
// Heavy payloads ride in _meta (delivered to the widget, kept out of the model context).
function readToolMeta(result) {
  return (result && (result._meta || result.meta)) || {};
}
function b64ToBytes(b64) {
  const bin = atob(b64), len = bin.length, bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}
const statusEl = document.getElementById("status");
const pagesEl  = document.getElementById("pages");
const fbEl     = document.getElementById("fallback");

function showFallback(text, note) {
  pagesEl.classList.add("hidden");
  fbEl.classList.remove("hidden");
  fbEl.textContent = text || "(no extractable text)";
  statusEl.textContent = note || "PDF preview unavailable – text version:";
}

async function renderPdf(bytes) {
  const pdfjsLib = window.pdfjsLib;
  if (!pdfjsLib) throw new Error("pdf.js not loaded (CSP?)");
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
  const pdf = await pdfjsLib.getDocument({ data: bytes }).promise;
  statusEl.textContent = pdf.numPages + " page(s)";
  for (let n = 1; n <= pdf.numPages; n++) {
    const page = await pdf.getPage(n);
    const viewport = page.getViewport({ scale: 1.5 });
    const canvas = document.createElement("canvas");
    canvas.width = viewport.width; canvas.height = viewport.height;
    pagesEl.append(canvas);
    await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
  }
}

const app = new App({ name: "DicomPdfView", version: "1.0.0" });
app.ontoolresult = async (result) => {
  const data = readToolData(result);
  if (!data.success) { showFallback("", data.message || "Could not load the PDF."); return; }
  // render_pdf_from_dicom hands us only the UIDs; pull the heavy payload (base64 + text) via a
  // widget-side tool call so it never enters the model's context.
  let payload;
  try {
    const res = await app.callServerTool({
      name: "get_pdf_payload",
      arguments: { study_instance_uid: data.study_instance_uid,
                   series_instance_uid: data.series_instance_uid,
                   sop_instance_uid: data.sop_instance_uid },
    });
    const pm = readToolMeta(res);
    payload = (pm && (pm.pdf_base64 || pm.text_content)) ? pm : readToolData(res);   // heavy payload rides in _meta
  } catch (e) {
    showFallback("", "Could not fetch the PDF (" + e + ")");
    return;
  }
  if (!payload.success || !payload.pdf_base64) {
    showFallback(payload.text_content, payload.message || "Could not load the PDF.");
    return;
  }
  try {
    await renderPdf(b64ToBytes(payload.pdf_base64));
  } catch (e) {
    showFallback(payload.text_content, "PDF rendering failed (" + e + ") – text version:");
  }
};

await app.connect();
const ctx = app.getHostContext?.();
if (ctx?.theme === "dark") document.documentElement.classList.add("dark");
app.onhostcontextchanged = (c) => {
  // Partial update: only act on fields actually present (a theme-only update must not flip mode,
  // a mode-only update must not flip the theme).
  if (c?.theme) document.documentElement.classList.toggle("dark", c.theme === "dark");
  // Gallery only: host left fullscreen (e.g. user hit the host's close-X) -> tear the lightbox
  // down so we land back on the thumbnail grid instead of a stuck single-image view.
  if (c?.displayMode && c.displayMode !== "fullscreen" && typeof resetView === "function") resetView();
};
</script>
</body>
</html>
"""


# Image gallery widget: shows the VL Endoscopic Images of a series as a thumbnail grid
# with a click-to-enlarge lightbox. Images arrive as base64 data URLs (original JPEG where
# possible), so no external image domains are needed - only the ext-apps runtime from unpkg.
IMAGE_GALLERY_WIDGET_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8">
<style>
  :root { color-scheme: light dark; }
  body { font: 14px system-ui, sans-serif; margin: 0; padding: 12px;
         background: #f7f7f8; color: #111; }
  html.dark body { background: #1e1e20; color: #eee; }
  h1 { font-size: 14px; margin: 0 0 10px; }
  #status { color: #666; font-size: 13px; margin-bottom: 10px; }
  html.dark #status { color: #aaa; }
  #grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; }
  .thumb { position: relative; aspect-ratio: 4 / 3; border: 1px solid #ddd; border-radius: 8px;
           overflow: hidden; background: #000; cursor: zoom-in; }
  html.dark .thumb { border-color: #444; }
  .thumb img { width: 100%; height: 100%; object-fit: contain; display: block; }
  .thumb .n { position: absolute; left: 6px; top: 6px; background: rgba(0,0,0,.6);
              color: #fff; font-size: 11px; padding: 1px 6px; border-radius: 6px; }
  #lb { position: fixed; inset: 0; background: rgba(0,0,0,.9); display: none;
        align-items: center; justify-content: center; cursor: zoom-out; padding: 16px; }
  #lb.open { display: flex; }
  #lb img { max-width: 100%; max-height: 100%; object-fit: contain; }
  #lbstatus { position: absolute; bottom: 18px; left: 50%; transform: translateX(-50%);
              color: #fff; font-size: 14px; background: rgba(0,0,0,.62); padding: 7px 16px;
              border-radius: 999px; pointer-events: none; white-space: nowrap; }
  /* While a full image is shown (esp. in fullscreen) hide the grid so only the image shows. */
  body.viewing h1, body.viewing #status, body.viewing #grid { display: none; }
  #lb.open { min-height: 100vh; }
</style></head>
<body>
  <h1>🩺 Endoscopy Images</h1>
  <div id="status">Loading images &hellip;</div>
  <div id="grid"></div>
  <div id="lb"><img id="lbimg" alt=""><div id="lbstatus"></div></div>
<script type="module">
import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";

function readToolData(result) {
  if (result && result.structuredContent && typeof result.structuredContent === "object")
    return result.structuredContent;
  const t = (result?.content || []).find(c => c.type === "text");
  try { return JSON.parse(t?.text || "{}"); } catch (e) { return {}; }
}
// Heavy payloads ride in _meta (delivered to the widget, kept out of the model context).
function readToolMeta(result) {
  return (result && (result._meta || result.meta)) || {};
}
const statusEl = document.getElementById("status");
const grid = document.getElementById("grid");
const lb = document.getElementById("lb");
const lbimg = document.getElementById("lbimg");
const lbStatus = document.getElementById("lbstatus");
// DOM teardown only (no display-mode request) so it is safe to call both when WE close the
// lightbox and when the HOST closes fullscreen via its own chrome.
function resetView() {
  lb.classList.remove("open");
  document.body.classList.remove("viewing");
}
lb.addEventListener("click", () => {
  resetView();
  requestMode("inline");   // we initiated the close -> ask the host to shrink back to inline
});

const app = new App({ name: "DicomImageGallery", version: "1.0.0" });
let STUDY = "", SERIES = "";

// Ask the host to grow the widget; falls back silently if display modes aren't supported.
async function requestMode(mode) {
  try { await app.requestDisplayMode({ mode }); } catch (e) { /* host may not support it */ }
}

async function openFull(sop, thumbSrc) {
  await requestMode("fullscreen");      // so the full image isn't capped by the inline widget size
  document.body.classList.add("viewing");  // hide the thumbnail grid behind the full image
  lbimg.src = thumbSrc;                 // show the thumbnail immediately while the full loads
  // Steer the user to close by clicking the image (smooth API exit, no chat scroll jump) rather
  // than the host's fullscreen close-X (which causes a one-time scroll jump on first exit).
  lbStatus.textContent = "Loading full image … · click the image to close";
  lb.classList.add("open");
  try {
    const res = await app.callServerTool({
      name: "get_single_image",
      arguments: { study_instance_uid: STUDY, series_instance_uid: SERIES, sop_instance_uid: sop },
    });
    const dm = readToolMeta(res);
    const d = (dm && dm.image_base64) ? dm : readToolData(res);   // base64 rides in _meta
    if (d.image_base64) {
      lbimg.src = "data:" + (d.mime_type || "image/jpeg") + ";base64," + d.image_base64;
      lbStatus.textContent = "Click the image to close";
    } else {
      lbStatus.textContent = d.message || "Full image unavailable";
    }
  } catch (e) {
    lbStatus.textContent = "Load error (" + e + ")";
  }
}

app.ontoolresult = async (result) => {
  const meta = readToolData(result);
  STUDY = meta.study_instance_uid || "";
  SERIES = meta.series_instance_uid || "";
  if (!meta.success) { statusEl.textContent = meta.message || "No images found."; return; }
  // render_images_from_dicom gives us only the UIDs; pull the thumbnail grid via a widget-side
  // call so the base64 never enters the model's context.
  let data;
  try {
    const res = await app.callServerTool({
      name: "get_image_thumbnails",
      arguments: { study_instance_uid: STUDY, series_instance_uid: SERIES },
    });
    const m = readToolMeta(res);
    data = (m && m.images) ? m : readToolData(res);   // heavy data rides in _meta; fall back to structuredContent
  } catch (e) {
    statusEl.textContent = "Could not load images (" + e + ")";
    return;
  }
  const images = (data.images || []).filter(im => im.image_base64);
  if (!data.success || images.length === 0) {
    statusEl.textContent = data.message || "No images found.";
    return;
  }
  statusEl.textContent = images.length + " image(s) – click to enlarge";
  grid.innerHTML = "";
  for (const im of images) {
    const src = "data:" + (im.mime_type || "image/jpeg") + ";base64," + im.image_base64;
    const cell = document.createElement("div");
    cell.className = "thumb";
    cell.innerHTML = '<span class="n">#' + (im.instance_number || "?") + "</span>";
    const img = document.createElement("img");
    img.src = src; img.alt = "Frame " + (im.instance_number || "");
    cell.appendChild(img);
    cell.addEventListener("click", () => openFull(im.sop_instance_uid, src));
    grid.appendChild(cell);
  }
};

await app.connect();
const ctx = app.getHostContext?.();
if (ctx?.theme === "dark") document.documentElement.classList.add("dark");
app.onhostcontextchanged = (c) => {
  // Partial update: only act on fields actually present (a theme-only update must not flip mode,
  // a mode-only update must not flip the theme).
  if (c?.theme) document.documentElement.classList.toggle("dark", c.theme === "dark");
  // Gallery only: host left fullscreen (e.g. user hit the host's close-X) -> tear the lightbox
  // down so we land back on the thumbnail grid instead of a stuck single-image view.
  if (c?.displayMode && c.displayMode !== "fullscreen" && typeof resetView === "function") resetView();
};
</script>
</body>
</html>
"""


# Single-image viewer: shows ONE full-resolution image inline, click to toggle fullscreen.
# Mirrors the gallery lightbox but as the primary view; lazy-loads the image via get_single_image.
SINGLE_IMAGE_WIDGET_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  :root { color-scheme: light dark; }
  body { font: 14px system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0; padding: 12px;
         color: #1a1a1a; }
  html.dark body { color: #e8e8e8; }
  h1 { font-size: 15px; margin: 0 0 4px; }
  #status { color: #666; font-size: 13px; margin-bottom: 8px; }
  html.dark #status { color: #aaa; }
  #img { max-width: 100%; height: auto; border-radius: 6px; display: block; cursor: zoom-in; }
  /* Fullscreen: image fills the viewport on a black backdrop; chrome hidden. */
  body.fs { background: #000; padding: 0; }
  body.fs h1, body.fs #status { display: none; }
  body.fs #img { position: fixed; inset: 0; margin: auto; max-width: 100vw; max-height: 100vh;
                 width: auto; height: auto; object-fit: contain; cursor: zoom-out; border-radius: 0; }
  #hint { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); color: #fff;
          background: rgba(0,0,0,.62); padding: 7px 16px; border-radius: 999px; font-size: 14px;
          display: none; pointer-events: none; white-space: nowrap; }
  body.fs #hint { display: block; }
</style></head>
<body>
  <h1>🩺 Endoscopy Image</h1>
  <div id="status">Loading image &hellip;</div>
  <img id="img" alt="">
  <div id="hint">Click the image to close</div>
<script type="module">
import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";

function readToolData(result) {
  if (result && result.structuredContent && typeof result.structuredContent === "object")
    return result.structuredContent;
  const t = (result?.content || []).find(c => c.type === "text");
  try { return JSON.parse(t?.text || "{}"); } catch (e) { return {}; }
}
// Heavy payloads ride in _meta (delivered to the widget, kept out of the model context).
function readToolMeta(result) {
  return (result && (result._meta || result.meta)) || {};
}
const statusEl = document.getElementById("status");
const imgEl = document.getElementById("img");

const app = new App({ name: "DicomSingleImage", version: "1.0.0" });
let fs = false;
async function requestMode(mode) {
  try { await app.requestDisplayMode({ mode }); } catch (e) { /* host may not support it */ }
}
function exitFs() { if (fs) { fs = false; document.body.classList.remove("fs"); } }

imgEl.addEventListener("click", async () => {
  if (!fs) { fs = true; document.body.classList.add("fs"); await requestMode("fullscreen"); }
  else { exitFs(); await requestMode("inline"); }   // smooth API exit (no chat scroll jump)
});

app.ontoolresult = async (result) => {
  const meta = readToolData(result);
  if (!meta.success) { statusEl.textContent = meta.message || "No image."; return; }
  // render_single_image_from_dicom gives us only the UIDs; pull the full image via a widget-side call
  // so the base64 never enters the model's context.
  let d;
  try {
    const res = await app.callServerTool({
      name: "get_single_image",
      arguments: { study_instance_uid: meta.study_instance_uid,
                   series_instance_uid: meta.series_instance_uid,
                   sop_instance_uid: meta.sop_instance_uid },
    });
    const dm = readToolMeta(res);
    d = (dm && dm.image_base64) ? dm : readToolData(res);   // base64 rides in _meta
  } catch (e) {
    statusEl.textContent = "Could not load image (" + e + ")";
    return;
  }
  if (!d.image_base64) { statusEl.textContent = d.message || "Image unavailable"; return; }
  imgEl.src = "data:" + (d.mime_type || "image/jpeg") + ";base64," + d.image_base64;
  const dim = (d.columns && d.rows) ? "  (" + d.columns + "\\u00d7" + d.rows + ")" : "";
  statusEl.textContent = "Click the image to enlarge" + dim;
};

await app.connect();
const ctx = app.getHostContext?.();
if (ctx?.theme === "dark") document.documentElement.classList.add("dark");
app.onhostcontextchanged = (c) => {
  if (c?.theme) document.documentElement.classList.toggle("dark", c.theme === "dark");
  // Host left fullscreen (e.g. user hit the host's close-X) -> drop back to the inline view.
  if (c?.displayMode && c.displayMode !== "fullscreen") exitFs();
};
</script>
</body>
</html>
"""


def create_dicom_mcp_server(config_path: str, name: str = "DICOM MCP") -> FastMCP:
    """Create and configure a DICOM MCP server."""
    
    # Define a simple lifespan function
    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[DicomContext]:
        # Load config
        config = load_config(config_path)
        
        # Get the current node and calling AE title
        current_node = config.nodes[config.current_node]
        
        # Create client
        client = DicomClient(
            host=current_node.host,
            port=current_node.port,
            calling_aet=config.calling_aet,
            called_aet=current_node.ae_title
        )
        
        logger.info(f"DICOM client initialized: {config.current_node} (calling AE: {config.calling_aet})")
        
        try:
            yield DicomContext(config=config, client=client)
        finally:
            pass
    
    # Create server
    mcp = FastMCP(name, lifespan=lifespan)
    
    # Register tools
    @mcp.tool()
    def list_dicom_nodes(ctx: Context = None) -> Dict[str, Any]:
        """List all configured DICOM nodes and their connection information.
        
        This tool returns information about all configured DICOM nodes in the system
        and shows which node is currently selected for operations. It also provides
        information about available calling AE titles.
        
        Returns:
            Dictionary containing:
            - current_node: The currently selected DICOM node name
            - nodes: List of all configured node names
        
        Example:
            {
                "current_node": "pacs1",
                "nodes": ["pacs1", "pacs2", "orthanc"],
            }
        """
        dicom_ctx = ctx.request_context.lifespan_context
        config = dicom_ctx.config
        
        current_node =  config.current_node
        nodes = [{node_name: node.description} for node_name, node in config.nodes.items()]

        return {
            "current_node": current_node,
            "nodes": nodes,
        }
    
    @mcp.tool()
    def extract_pdf_text_from_dicom(
        study_instance_uid: str,
        series_instance_uid: str,
        sop_instance_uid: str,
        ctx: Context = None
    ) -> Dict[str, Any]:
        """Retrieve a DICOM instance with encapsulated PDF and extract its text content.
        
        This tool retrieves a DICOM instance containing an encapsulated PDF document,
        extracts the PDF, and converts it to text. This is particularly useful for
        medical reports stored as PDFs within DICOM format (e.g., radiology reports,
        clinical documents).
        
        Args:
            study_instance_uid: The unique identifier for the study (required)
            series_instance_uid: The unique identifier for the series within the study (required)
            sop_instance_uid: The unique identifier for the specific DICOM instance (required)
        
        Returns:
            Dictionary containing:
            - success: Boolean indicating if the operation was successful
            - message: Description of the operation result or error
            - text_content: The extracted text from the PDF (if successful)
            - file_path: Path to the temporary DICOM file (for debugging purposes)
        
        Example:
            {
                "success": true,
                "message": "Successfully extracted text from PDF in DICOM",
                "text_content": "Patient report contents...",
                "file_path": "/tmp/tmpdir123/1.2.3.4.5.6.7.8.dcm"
            }
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client:DicomClient = dicom_ctx.client
        
        return client.extract_pdf_text_from_dicom(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            sop_instance_uid=sop_instance_uid
        )

    @mcp.tool()
    def switch_dicom_node(node_name: str, ctx: Context = None) -> Dict[str, Any]:
        """Switch the active DICOM node connection to a different configured node.
        
        This tool changes which DICOM node (PACS, workstation, etc.) subsequent operations
        will connect to. The node must be defined in the configuration file.
        
        Args:
            node_name: The name of the node to switch to, must match a name in the configuration
        
        Returns:
            Dictionary containing:
            - success: Boolean indicating if the switch was successful
            - message: Description of the operation result or error
        
        Example:
            {
                "success": true,
                "message": "Switched to DICOM node: orthanc"
            }
        
        Raises:
            ValueError: If the specified node name is not found in configuration
        """        
        dicom_ctx = ctx.request_context.lifespan_context
        config = dicom_ctx.config
        
        # Check if node exists
        if node_name not in config.nodes:
            raise ValueError(f"Node '{node_name}' not found in configuration")
        
        # Update configuration
        config.current_node = node_name
        
        # Create a new client with the updated configuration
        current_node = config.nodes[config.current_node]
        
        # Replace the client with a new instance
        dicom_ctx.client = DicomClient(
            host=current_node.host,
            port=current_node.port,
            calling_aet=config.calling_aet,
            called_aet=current_node.ae_title
        )
        
        return {
            "success": True,
            "message": f"Switched to DICOM node: {node_name}"
        }

    @mcp.tool()
    def verify_connection(ctx: Context = None) -> str:
        """Verify connectivity to the current DICOM node using C-ECHO.
        
        This tool performs a DICOM C-ECHO operation (similar to a network ping) to check
        if the currently selected DICOM node is reachable and responds correctly. This is
        useful to troubleshoot connection issues before attempting other operations.
        
        Returns:
            A message describing the connection status, including host, port, and AE titles
        
        Example:
            "Connection successful to 192.168.1.100:104 (Called AE: ORTHANC, Calling AE: CLIENT)"
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client = dicom_ctx.client
        
        success, message = client.verify_connection()
        return message

    @mcp.tool()
    def query_patients(
        name_pattern: str = "", 
        patient_id: str = "", 
        birth_date: str = "", 
        attribute_preset: str = "standard", 
        additional_attributes: List[str] = None,
        exclude_attributes: List[str] = None, 
        ctx: Context = None
    ) -> List[Dict[str, Any]]:
        """Query patients matching the specified criteria from the DICOM node.
        
        This tool performs a DICOM C-FIND operation at the PATIENT level to find patients
        matching the provided search criteria. All search parameters are optional and can
        be combined for more specific queries.
        
        Args:
            name_pattern: Patient name pattern (can include wildcards * and ?), e.g., "SMITH*"
            patient_id: Patient ID to search for, e.g., "12345678"
            birth_date: Patient birth date in YYYYMMDD format, e.g., "19700101"
            attribute_preset: Controls which attributes to include in results:
                - "minimal": Only essential attributes
                - "standard": Common attributes (default)
                - "extended": All available attributes
            additional_attributes: List of specific DICOM attributes to include beyond the preset
            exclude_attributes: List of DICOM attributes to exclude from the results
        
        Returns:
            List of dictionaries, each representing a matched patient with their attributes
        
        Example:
            [
                {
                    "PatientID": "12345",
                    "PatientName": "SMITH^JOHN",
                    "PatientBirthDate": "19700101",
                    "PatientSex": "M"
                }
            ]
        
        Raises:
            Exception: If there is an error communicating with the DICOM node
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client = dicom_ctx.client
        
        try:
            return client.query_patient(
                patient_id=patient_id,
                name_pattern=name_pattern,
                birth_date=birth_date,
                attribute_preset=attribute_preset,
                additional_attrs=additional_attributes,
                exclude_attrs=exclude_attributes
            )
        except Exception as e:
            raise Exception(f"Error querying patients: {str(e)}")

    @mcp.tool()
    def query_studies(
        patient_id: str = "", 
        study_date: str = "", 
        modality_in_study: str = "",
        study_description: str = "", 
        accession_number: str = "", 
        study_instance_uid: str = "",
        attribute_preset: str = "standard", 
        additional_attributes: List[str] = None,
        exclude_attributes: List[str] = None, 
        ctx: Context = None
    ) -> List[Dict[str, Any]]:
        """Query studies matching the specified criteria from the DICOM node.
        
        This tool performs a DICOM C-FIND operation at the STUDY level to find studies
        matching the provided search criteria. All search parameters are optional and can
        be combined for more specific queries.
        
        Args:
            patient_id: Patient ID to search for, e.g., "12345678"
            study_date: Study date or date range in DICOM format:
                - Single date: "20230101"
                - Date range: "20230101-20230131"
            modality_in_study: Filter by modalities present in study, e.g., "CT" or "MR"
            study_description: Study description text (can include wildcards), e.g., "CHEST*"
            accession_number: Medical record accession number
            study_instance_uid: Unique identifier for a specific study
            attribute_preset: Controls which attributes to include in results:
                - "minimal": Only essential attributes
                - "standard": Common attributes (default)
                - "extended": All available attributes
            additional_attributes: List of specific DICOM attributes to include beyond the preset
            exclude_attributes: List of DICOM attributes to exclude from the results
        
        Returns:
            List of dictionaries, each representing a matched study with its attributes
        
        Example:
            [
                {
                    "StudyInstanceUID": "1.2.840.113619.2.1.1.322.1600364094.412.1009",
                    "StudyDate": "20230215",
                    "StudyDescription": "CHEST CT",
                    "PatientID": "12345",
                    "PatientName": "SMITH^JOHN",
                    "ModalitiesInStudy": "CT"
                }
            ]
        
        Raises:
            Exception: If there is an error communicating with the DICOM node
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client = dicom_ctx.client
        
        try:
            return client.query_study(
                patient_id=patient_id,
                study_date=study_date,
                modality=modality_in_study,
                study_description=study_description,
                accession_number=accession_number,
                study_instance_uid=study_instance_uid,
                attribute_preset=attribute_preset,
                additional_attrs=additional_attributes,
                exclude_attrs=exclude_attributes
            )
        except Exception as e:
            raise Exception(f"Error querying studies: {str(e)}")

    @mcp.tool()
    def query_series(
        study_instance_uid: str, 
        modality: str = "", 
        series_number: str = "",
        series_description: str = "", 
        series_instance_uid: str = "",
        attribute_preset: str = "standard", 
        additional_attributes: List[str] = None,
        exclude_attributes: List[str] = None, 
        ctx: Context = None
    ) -> List[Dict[str, Any]]:
        """Query series within a study from the DICOM node.
        
        This tool performs a DICOM C-FIND operation at the SERIES level to find series
        within a specified study. The study_instance_uid is required, and additional
        parameters can be used to filter the results.
        
        Args:
            study_instance_uid: Unique identifier for the study (required)
            modality: Filter by imaging modality, e.g., "CT", "MR", "US", "CR"
            series_number: Filter by series number
            series_description: Series description text (can include wildcards), e.g., "AXIAL*"
            series_instance_uid: Unique identifier for a specific series
            attribute_preset: Controls which attributes to include in results:
                - "minimal": Only essential attributes
                - "standard": Common attributes (default)
                - "extended": All available attributes
            additional_attributes: List of specific DICOM attributes to include beyond the preset
            exclude_attributes: List of DICOM attributes to exclude from the results
        
        Returns:
            List of dictionaries, each representing a matched series with its attributes
        
        Example:
            [
                {
                    "SeriesInstanceUID": "1.2.840.113619.2.1.1.322.1600364094.412.2005",
                    "SeriesNumber": "2",
                    "SeriesDescription": "AXIAL 2.5MM",
                    "Modality": "CT",
                    "NumberOfSeriesRelatedInstances": "120"
                }
            ]
        
        Raises:
            Exception: If there is an error communicating with the DICOM node
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client = dicom_ctx.client
        
        try:
            return client.query_series(
                study_instance_uid=study_instance_uid,
                series_instance_uid=series_instance_uid,
                modality=modality,
                series_number=series_number,
                series_description=series_description,
                attribute_preset=attribute_preset,
                additional_attrs=additional_attributes,
                exclude_attrs=exclude_attributes
            )
        except Exception as e:
            raise Exception(f"Error querying series: {str(e)}")

    @mcp.tool()
    def query_instances(
        series_instance_uid: str, 
        instance_number: str = "", 
        sop_instance_uid: str = "",
        attribute_preset: str = "standard", 
        additional_attributes: List[str] = None,
        exclude_attributes: List[str] = None, 
        ctx: Context = None 
    ) -> List[Dict[str, Any]]:
        """Query individual DICOM instances (images) within a series.
        
        This tool performs a DICOM C-FIND operation at the IMAGE level to find individual
        DICOM instances within a specified series. The series_instance_uid is required,
        and additional parameters can be used to filter the results.
        
        Args:
            series_instance_uid: Unique identifier for the series (required)
            instance_number: Filter by specific instance number within the series
            sop_instance_uid: Unique identifier for a specific instance
            attribute_preset: Controls which attributes to include in results:
                - "minimal": Only essential attributes
                - "standard": Common attributes (default)
                - "extended": All available attributes
            additional_attributes: List of specific DICOM attributes to include beyond the preset
            exclude_attributes: List of DICOM attributes to exclude from the results
        
        Returns:
            List of dictionaries, each representing a matched instance with its attributes
        
        Example:
            [
                {
                    "SOPInstanceUID": "1.2.840.113619.2.1.1.322.1600364094.412.3001",
                    "SOPClassUID": "1.2.840.10008.5.1.4.1.1.2",
                    "InstanceNumber": "45",
                    "ContentDate": "20230215",
                    "ContentTime": "152245"
                }
            ]
        
        Raises:
            Exception: If there is an error communicating with the DICOM node
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client = dicom_ctx.client
        
        try:
            return client.query_instance(
                series_instance_uid=series_instance_uid,
                sop_instance_uid=sop_instance_uid,
                instance_number=instance_number,
                attribute_preset=attribute_preset,
                additional_attrs=additional_attributes,
                exclude_attrs=exclude_attributes
            )
        except Exception as e:
            raise Exception(f"Error querying instances: {str(e)}")
        
    @mcp.tool()
    def move_series(
        destination_node: str,
        series_instance_uid: str,
        ctx: Context = None
    ) -> Dict[str, Any]:
        """Move a DICOM series to another DICOM node.
        
        This tool transfers a specific series from the current DICOM server to a 
        destination DICOM node.
        
        Args:
            destination_node: Name of the destination node as defined in the configuration
            series_instance_uid: The unique identifier for the series to be moved
        
        Returns:
            Dictionary containing:
            - success: Boolean indicating if the operation was successful
            - message: Description of the operation result or error
            - completed: Number of successfully transferred instances
            - failed: Number of failed transfers
            - warning: Number of transfers with warnings
        
        Example:
            {
                "success": true,
                "message": "C-MOVE operation completed successfully",
                "completed": 120,
                "failed": 0,
                "warning": 0
            }
        """
        dicom_ctx = ctx.request_context.lifespan_context
        config = dicom_ctx.config
        client = dicom_ctx.client
        
        # Check if destination node exists
        if destination_node not in config.nodes:
            raise ValueError(f"Destination node '{destination_node}' not found in configuration")
        
        # Get the destination AE title
        destination_ae = config.nodes[destination_node].ae_title
        
        # Execute the move operation
        result = client.move_series(
            destination_ae=destination_ae,
            series_instance_uid=series_instance_uid
        )
        
        return result

    @mcp.tool()
    def move_study(
        destination_node: str,
        study_instance_uid: str,
        ctx: Context = None
    ) -> Dict[str, Any]:
        """Move a DICOM study to another DICOM node.
        
        This tool transfers an entire study from the current DICOM server to a 
        destination DICOM node.
        
        Args:
            destination_node: Name of the destination node as defined in the configuration
            study_instance_uid: The unique identifier for the study to be moved
        
        Returns:
            Dictionary containing:
            - success: Boolean indicating if the operation was successful
            - message: Description of the operation result or error
            - completed: Number of successfully transferred instances
            - failed: Number of failed transfers
            - warning: Number of transfers with warnings
        
        Example:
            {
                "success": true,
                "message": "C-MOVE operation completed successfully",
                "completed": 256,
                "failed": 0,
                "warning": 0
            }
        """
        dicom_ctx = ctx.request_context.lifespan_context
        config = dicom_ctx.config
        client = dicom_ctx.client
        
        # Check if destination node exists
        if destination_node not in config.nodes:
            raise ValueError(f"Destination node '{destination_node}' not found in configuration")
        
        # Get the destination AE title
        destination_ae = config.nodes[destination_node].ae_title
        
        # Execute the move operation
        result = client.move_study(
            destination_ae=destination_ae,
            study_instance_uid=study_instance_uid
        )
        
        return result


    @mcp.tool()
    def get_attribute_presets() -> Dict[str, Dict[str, List[str]]]:
        """Get all available attribute presets for DICOM queries.
        
        This tool returns the defined attribute presets that can be used with the
        query_* functions. It shows which DICOM attributes are included in each
        preset (minimal, standard, extended) for each query level.
        
        Returns:
            Dictionary organized by query level (patient, study, series, instance),
            with each level containing the attribute presets and their associated
            DICOM attributes.
        
        Example:
            {
                "patient": {
                    "minimal": ["PatientID", "PatientName"],
                    "standard": ["PatientID", "PatientName", "PatientBirthDate", "PatientSex"],
                    "extended": ["PatientID", "PatientName", "PatientBirthDate", "PatientSex", ...]
                },
                "study": {
                    "minimal": ["StudyInstanceUID", "StudyDate"],
                    "standard": ["StudyInstanceUID", "StudyDate", "StudyDescription", ...],
                    "extended": ["StudyInstanceUID", "StudyDate", "StudyDescription", ...]
                },
                ...
            }
        """
        return ATTRIBUTE_PRESETS

    # --- PDF viewer widget ------------------------------------------------------
    @mcp.tool(app=AppConfig(resource_uri="ui://dicom/pdf-view.html"))
    def render_pdf_from_dicom(
        study_instance_uid: str,
        series_instance_uid: str,
        sop_instance_uid: str,
        ctx: Context = None,
    ) -> ToolResult:
        """Render a DICOM-encapsulated PDF report inline (PDF viewer widget).

        Use this when the user wants to *see* the report, not just read its text. This places the
        ui://dicom/pdf-view.html widget and hands it the three UIDs; the widget then lazy-loads the
        PDF bytes + extracted text itself via get_pdf_payload and renders the pages with pdf.js.
        The heavy payload (base64 + text) is fetched by the widget on purpose, so it never enters
        the model's context - for plain report text use extract_pdf_text_from_dicom instead.

        IMPORTANT: once this returns, the report is ALREADY shown to the user in the widget. Do not
        reproduce the report text, save it to a file, or call other tools - just briefly confirm.

        Get the three UIDs from a query first (query_studies -> query_series ->
        query_instances); the target must be an Encapsulated PDF instance.

        Args:
            study_instance_uid: Study Instance UID
            series_instance_uid: Series Instance UID
            sop_instance_uid: SOP Instance UID
        """
        # Return ONLY the UIDs (no base64/text/file_path) so nothing tempting reaches the model -
        # the widget pulls the payload via get_pdf_payload, which hands the heavy base64+text back in
        # _meta (delivered to the widget but NOT surfaced to the model). NB: Claude Desktop DOES
        # surface a widget-initiated call's content/structured_content to the model, so heavy fields
        # must go in _meta, not structured_content - otherwise the model reprints/saves them.
        data = {
            "success": True,
            "study_instance_uid": study_instance_uid,
            "series_instance_uid": series_instance_uid,
            "sop_instance_uid": sop_instance_uid,
        }
        summary = ("PDF report is NOW rendered inline for the user in the PDF viewer widget. Reply "
                   "with a brief, user-facing confirmation only; do NOT reproduce the report text, "
                   "save it to a file, or call other tools - the widget already shows it.")
        return ToolResult(content=summary, structured_content=data)

    @mcp.tool()
    def get_pdf_payload(
        study_instance_uid: str,
        series_instance_uid: str,
        sop_instance_uid: str,
        ctx: Context = None,
    ) -> ToolResult:
        """Retrieve the PDF bytes + extracted text for the PDF viewer widget (widget-internal).

        Called by the ui://dicom/pdf-view.html widget via callServerTool to lazy-load the report
        payload after render_pdf_from_dicom has placed the widget. Do NOT call this directly - to
        show a report use render_pdf_from_dicom, to read its text use extract_pdf_text_from_dicom.

        Args:
            study_instance_uid: Study Instance UID
            series_instance_uid: Series Instance UID
            sop_instance_uid: SOP Instance UID of the Encapsulated PDF instance
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client: DicomClient = dicom_ctx.client
        data = client.get_pdf_from_dicom(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            sop_instance_uid=sop_instance_uid,
        )
        payload = {
            "success": data.get("success", False),
            "message": data.get("message", ""),
            "pdf_base64": data.get("pdf_base64", ""),
            "text_content": data.get("text_content", ""),
        }
        # PDF bytes AND the extracted report text ride in _meta (delivered to the widget, kept out of
        # the model context - otherwise the model reproduces the report). structured_content stays slim.
        slim = {"success": payload["success"], "message": payload["message"]}
        return ToolResult(content="(PDF payload delivered to the widget.)",
                          structured_content=slim, meta=payload)

    @mcp.resource(
        "ui://dicom/pdf-view.html",
        app=AppConfig(csp=ResourceCSP(
            resource_domains=["https://unpkg.com", "https://cdnjs.cloudflare.com"],
            connect_domains=["https://cdnjs.cloudflare.com"],
        )),
    )
    def pdf_view_widget() -> str:
        """HTML for the render_pdf_from_dicom PDF viewer UI."""
        return PDF_VIEW_WIDGET_HTML

    # --- Image gallery widget ---------------------------------------------------
    @mcp.tool(app=AppConfig(resource_uri="ui://dicom/image-gallery.html"))
    def render_images_from_dicom(
        study_instance_uid: str,
        series_instance_uid: str,
        ctx: Context = None,
    ) -> ToolResult:
        """Show the VL Endoscopic Images of a series as an inline gallery (gallery widget).

        Use this when the user wants to *see* the endoscopy images. This places the
        ui://dicom/image-gallery.html widget and hands it the two UIDs; the widget then lazy-loads
        the thumbnail grid itself via get_image_thumbnails (and the full image per thumbnail via
        get_single_image). The thumbnails are fetched by the widget on purpose, so the base64 never
        enters the model's context.

        IMPORTANT: once this returns, the images are ALREADY shown to the user in the gallery
        widget. Do NOT call any further tool (visualization/artifact/code tools, get_image_thumbnails
        or get_single_image) to show, render or save them, and do not describe their contents unless
        asked - just briefly confirm. Only act further if the user explicitly asks.

        Get the UIDs from a query first (query_studies -> query_series); point at the image
        series (Modality ES / VL Endoscopic Image), not the report (SR/PDF) series.

        Args:
            study_instance_uid: Study Instance UID
            series_instance_uid: Series Instance UID
        """
        # Return ONLY the UIDs (no base64) so nothing heavy reaches the model - the widget pulls
        # the thumbnail grid via get_image_thumbnails, a widget-side call that stays out of the
        # model's context. (Claude Desktop surfaces structured_content to the model.)
        data = {
            "success": True,
            "study_instance_uid": study_instance_uid,
            "series_instance_uid": series_instance_uid,
        }
        summary = ("The endoscopy images of the series are NOW displayed to the user inline in the "
                   "gallery widget. Reply with a brief, user-facing confirmation only; do NOT save "
                   "them to files or call other tools - the widget already shows them.")
        return ToolResult(content=summary, structured_content=data)

    @mcp.tool()
    def get_image_thumbnails(
        study_instance_uid: str,
        series_instance_uid: str,
        ctx: Context = None,
    ) -> ToolResult:
        """Retrieve downscaled thumbnails of a series for the gallery widget (widget-internal).

        Called by the ui://dicom/image-gallery.html widget via callServerTool to lazy-load the
        thumbnail grid after render_images_from_dicom has placed the widget. Do NOT call this
        directly - to show images use render_images_from_dicom.

        Args:
            study_instance_uid: Study Instance UID
            series_instance_uid: Series Instance UID
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client: DicomClient = dicom_ctx.client
        data = client.get_images_from_dicom(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
        )
        # Claude Desktop surfaces a widget-initiated tool call's content/structured_content to the
        # MODEL. Putting the thumbnails (base64) AND the per-image sop_instance_uids there floods the
        # model context and - worse - lets the model proactively fetch every full image via
        # get_single_image. So the heavy payload rides in _meta (verified: delivered to the widget,
        # NOT to the model); structured_content keeps only a slim summary.
        slim = {"success": data.get("success"), "count": data.get("count"),
                "message": data.get("message")}
        return ToolResult(
            content="(thumbnails delivered to the widget.)",
            structured_content=slim,
            meta=data,
        )

    @mcp.tool()
    def get_single_image(
        study_instance_uid: str,
        series_instance_uid: str,
        sop_instance_uid: str,
        ctx: Context = None,
    ) -> ToolResult:
        """Retrieve one full-resolution endoscopy image (used by the gallery lightbox).

        Returns the full-resolution frame base64-encoded in structured_content. Primarily
        called by the image gallery widget when the user enlarges a thumbnail; to *show*
        images the model should use render_images_from_dicom instead.

        Args:
            study_instance_uid: Study Instance UID
            series_instance_uid: Series Instance UID
            sop_instance_uid: SOP Instance UID of the image to enlarge
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client: DicomClient = dicom_ctx.client
        data = client.get_single_image(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            sop_instance_uid=sop_instance_uid,
        )
        summary = ("Retrieved full-resolution image." if data.get("success")
                   else data.get("message", "No image."))
        # Heavy base64 rides in _meta (delivered to the widget, kept out of the model context);
        # structured_content stays slim. See get_image_thumbnails for the rationale.
        slim = {"success": data.get("success"), "message": data.get("message"),
                "sop_instance_uid": data.get("sop_instance_uid"),
                "rows": data.get("rows"), "columns": data.get("columns")}
        return ToolResult(content=summary, structured_content=slim, meta=data)

    # --- Download / save-to-disk tools ------------------------------------------
    @mcp.tool()
    def save_pdf_from_dicom(
        study_instance_uid: str,
        series_instance_uid: str,
        sop_instance_uid: str,
        destination: str,
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Download a DICOM-encapsulated PDF report to a local file.

        Use ONLY when the user explicitly asks to save/download the report. C-GETs the Encapsulated
        PDF instance and writes the PDF bytes to disk on the machine running this server (the user's
        machine). `destination` may be a target .pdf path or an existing directory (then a filename
        is derived from PatientID/StudyDate). `~` and environment variables are expanded.

        Args:
            study_instance_uid: Study Instance UID
            series_instance_uid: Series Instance UID
            sop_instance_uid: SOP Instance UID of the Encapsulated PDF instance
            destination: target file path or directory (e.g. "~/Downloads" or "~/Downloads/report.pdf")
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client: DicomClient = dicom_ctx.client
        return client.save_pdf(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            sop_instance_uid=sop_instance_uid,
            destination=destination,
        )

    @mcp.tool()
    def save_images_from_dicom(
        study_instance_uid: str,
        series_instance_uid: str,
        destination: str,
        sop_instance_uid: str = None,
        ctx: Context = None,
    ) -> Dict[str, Any]:
        """Download endoscopy image(s) of a series to local files (original quality).

        Use ONLY when the user explicitly asks to save/download image(s). C-GETs the VL Endoscopic
        Image(s) and writes the original JPEG bitstream where available (lossless), else PNG, on the
        machine running this server (the user's machine). `~` and environment variables are expanded.

        - Whole series: omit sop_instance_uid; `destination` is a directory, files are named
          image_01.jpg, image_02.jpg, ...
        - Single image: pass sop_instance_uid; `destination` may be a directory or an explicit file
          path.

        Args:
            study_instance_uid: Study Instance UID
            series_instance_uid: Series Instance UID
            destination: target directory (whole series) or directory/file path (single image)
            sop_instance_uid: SOP Instance UID of one image to save; omit to save the whole series
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client: DicomClient = dicom_ctx.client
        return client.save_images(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            destination=destination,
            sop_instance_uid=sop_instance_uid,
        )

    @mcp.resource(
        "ui://dicom/image-gallery.html",
        app=AppConfig(csp=ResourceCSP(resource_domains=["https://unpkg.com"])),
    )
    def image_gallery_widget() -> str:
        """HTML for the render_images_from_dicom gallery UI."""
        return IMAGE_GALLERY_WIDGET_HTML

    # --- Single-image viewer widget ---------------------------------------------
    @mcp.tool(app=AppConfig(resource_uri="ui://dicom/single-image.html"))
    def render_single_image_from_dicom(
        study_instance_uid: str,
        series_instance_uid: str,
        sop_instance_uid: str,
        ctx: Context = None,
    ) -> ToolResult:
        """Show ONE endoscopy image large inline (single-image viewer widget).

        Use this when the user wants to *see* a single specific image (not the whole series). This
        places the ui://dicom/single-image.html widget and hands it the three UIDs; the widget then
        lazy-loads the full-resolution image itself via get_single_image and shows it (click to
        toggle fullscreen). The base64 is fetched by the widget on purpose, so it never enters the
        model's context. For the whole series use render_images_from_dicom.

        IMPORTANT: once this returns, the image is ALREADY shown to the user in the widget. Do not
        save it to a file, describe its contents unless asked, or call other tools - just briefly
        confirm.

        Get the three UIDs from a query first (query_studies -> query_series -> query_instances);
        the target must be a VL Endoscopic Image instance.

        Args:
            study_instance_uid: Study Instance UID
            series_instance_uid: Series Instance UID
            sop_instance_uid: SOP Instance UID of the image to show
        """
        data = {
            "success": True,
            "study_instance_uid": study_instance_uid,
            "series_instance_uid": series_instance_uid,
            "sop_instance_uid": sop_instance_uid,
        }
        summary = ("The endoscopy image is NOW displayed to the user inline in the image viewer "
                   "widget. Reply with a brief, user-facing confirmation only; do NOT save it to a "
                   "file or call other tools - the widget already shows it.")
        return ToolResult(content=summary, structured_content=data)

    @mcp.resource(
        "ui://dicom/single-image.html",
        app=AppConfig(csp=ResourceCSP(resource_domains=["https://unpkg.com"])),
    )
    def single_image_widget() -> str:
        """HTML for the render_single_image_from_dicom single-image viewer UI."""
        return SINGLE_IMAGE_WIDGET_HTML

    return mcp