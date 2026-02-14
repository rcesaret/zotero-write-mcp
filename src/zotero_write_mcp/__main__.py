"""Entry point for zotero-write-mcp server."""
import sys

def main():
    from zotero_write_mcp.server import mcp
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
