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
    <h1>📄 DICOM-Bericht (Encapsulated PDF)</h1>
  </div>
  <div id="status">Lade PDF &hellip;</div>
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
  fbEl.textContent = text || "(kein Text extrahierbar)";
  statusEl.textContent = note || "PDF-Vorschau nicht verfügbar – Textfassung:";
}

async function renderPdf(bytes) {
  const pdfjsLib = window.pdfjsLib;
  if (!pdfjsLib) throw new Error("pdf.js nicht geladen (CSP?)");
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
  const pdf = await pdfjsLib.getDocument({ data: bytes }).promise;
  statusEl.textContent = pdf.numPages + " Seite(n)";
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
  if (!data.success || !data.pdf_base64) {
    showFallback(data.text_content, data.message || "PDF konnte nicht geladen werden.");
    return;
  }
  try {
    await renderPdf(b64ToBytes(data.pdf_base64));
  } catch (e) {
    showFallback(data.text_content, "PDF-Rendering fehlgeschlagen (" + e + ") – Textfassung:");
  }
};

await app.connect();
const ctx = app.getHostContext?.();
if (ctx?.theme === "dark") document.documentElement.classList.add("dark");
app.onhostcontextchanged = (c) =>
  document.documentElement.classList.toggle("dark", c?.theme === "dark");
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
  #lbstatus { position: absolute; bottom: 14px; left: 0; right: 0; text-align: center;
              color: #fff; font-size: 13px; }
  /* While a full image is shown (esp. in fullscreen) hide the grid so only the image shows. */
  body.viewing h1, body.viewing #status, body.viewing #grid { display: none; }
  #lb.open { min-height: 100vh; }
</style></head>
<body>
  <h1>🩺 Endoskopie-Bilder</h1>
  <div id="status">Lade Bilder &hellip;</div>
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
const statusEl = document.getElementById("status");
const grid = document.getElementById("grid");
const lb = document.getElementById("lb");
const lbimg = document.getElementById("lbimg");
const lbStatus = document.getElementById("lbstatus");
lb.addEventListener("click", () => {
  lb.classList.remove("open");
  document.body.classList.remove("viewing");
  requestMode("inline");
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
  lbStatus.textContent = "Lade Vollbild …";
  lb.classList.add("open");
  try {
    const res = await app.callServerTool({
      name: "get_single_image",
      arguments: { study_instance_uid: STUDY, series_instance_uid: SERIES, sop_instance_uid: sop },
    });
    const d = readToolData(res);
    if (d.image_base64) {
      lbimg.src = "data:" + (d.mime_type || "image/jpeg") + ";base64," + d.image_base64;
      lbStatus.textContent = "Klick zum Schließen";
    } else {
      lbStatus.textContent = d.message || "Vollbild nicht verfügbar";
    }
  } catch (e) {
    lbStatus.textContent = "Fehler beim Laden (" + e + ")";
  }
}

app.ontoolresult = (result) => {
  const data = readToolData(result);
  STUDY = data.study_instance_uid || "";
  SERIES = data.series_instance_uid || "";
  const images = (data.images || []).filter(im => im.image_base64);
  if (!data.success || images.length === 0) {
    statusEl.textContent = data.message || "Keine Bilder gefunden.";
    return;
  }
  statusEl.textContent = images.length + " Bild(er) – zum Vergrößern anklicken";
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
app.onhostcontextchanged = (c) =>
  document.documentElement.classList.toggle("dark", c?.theme === "dark");
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
    ) -> Dict[str, Any]:
        """Retrieve a DICOM-encapsulated PDF report and render it inline (PDF viewer).

        Use this when the user wants to *see* the report, not just read its text. Retrieves
        the Encapsulated PDF instance via C-GET and returns it base64-encoded so the
        ui://dicom/pdf-view.html widget renders the pages with pdf.js. Also returns the
        extracted text as a fallback for hosts that don't render widgets.

        Get the three UIDs from a query first (query_studies -> query_series ->
        query_instances); the target must be an Encapsulated PDF instance.

        Args:
            study_instance_uid: Study Instance UID
            series_instance_uid: Series Instance UID
            sop_instance_uid: SOP Instance UID

        Returns:
            Dictionary: { success, message, pdf_base64, size_bytes, text_content, file_path }
        """
        dicom_ctx = ctx.request_context.lifespan_context
        client: DicomClient = dicom_ctx.client
        return client.get_pdf_from_dicom(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            sop_instance_uid=sop_instance_uid,
        )

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
        """Retrieve the VL Endoscopic Images of a series and show them as an inline gallery.

        Use this when the user wants to *see* the endoscopy images. Retrieves every image of
        the series via C-GET, downscales them to thumbnails, and shows them in the
        ui://dicom/image-gallery.html widget (thumbnail grid + click-to-enlarge lightbox that
        lazy-loads the full image via get_single_image).

        Get the UIDs from a query first (query_studies -> query_series); point at the image
        series (Modality ES / VL Endoscopic Image), not the report (SR/PDF) series.

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
        # Keep the base64 thumbnails out of the model's context (a full gallery can exceed
        # the tool-result size limit) - the widget reads them from structured_content.
        summary = (f"Retrieved {data.get('count', 0)} endoscopy image(s); shown in the gallery widget."
                   if data.get("success") else data.get("message", "No images."))
        return ToolResult(content=summary, structured_content=data)

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
        return ToolResult(content=summary, structured_content=data)

    @mcp.resource(
        "ui://dicom/image-gallery.html",
        app=AppConfig(csp=ResourceCSP(resource_domains=["https://unpkg.com"])),
    )
    def image_gallery_widget() -> str:
        """HTML for the render_images_from_dicom gallery UI."""
        return IMAGE_GALLERY_WIDGET_HTML

    return mcp