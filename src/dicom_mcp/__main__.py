"""
Main entry point for the DICOM MCP Server.
"""
import argparse

from .server import create_dicom_mcp_server

def main():
    # Simple argument parser
    parser = argparse.ArgumentParser(description="DICOM Model Context Protocol Server")
    parser.add_argument("config_path", help="Path to the DICOM configuration YAML file")
    parser.add_argument("--transport", default="stdio",
                        help="MCP transport: 'stdio' (default), 'http', or 'sse'")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind host for the 'http'/'sse' transport (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000,
                        help="Bind port for the 'http'/'sse' transport (default 8000)")

    args = parser.parse_args()

    # Create and run the server. HTTP/SSE transports need a host+port to bind;
    # stdio is launched directly by the MCP client and ignores them.
    mcp = create_dicom_mcp_server(args.config_path)
    if args.transport in ("http", "streamable-http", "sse"):
        mcp.run(args.transport, host=args.host, port=args.port)
    else:
        mcp.run(args.transport)
    
if __name__ == "__main__":
    main()