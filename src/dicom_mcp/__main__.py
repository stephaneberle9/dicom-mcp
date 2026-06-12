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
        # stateless_http: treat every request independently (no session affinity). This makes
        # the server survive restarts while the client stays up: when we restart this server
        # but Claude + mcp-remote keep running, they must switch to the fresh server instance
        # on the fly. With a stateful server the old session id is gone after the restart, so
        # the reconnect fails with "server unreachable". Stateless binds nothing to a session,
        # so the reconnect just works.
        #
        # Trade-off: stateless drops long-lived MCP session features (server->client
        # notifications, resource subscriptions, per-session ctx state). This server uses none
        # of them -- tools answer synchronously and the DICOM client lives in the server
        # lifespan (app-wide, not session-bound), so it stays available across requests.
        # Revisit only if a future tool needs streaming/progress over a persistent session.
        mcp.run(args.transport, host=args.host, port=args.port, stateless_http=True)
    else:
        mcp.run(args.transport)
    
if __name__ == "__main__":
    main()