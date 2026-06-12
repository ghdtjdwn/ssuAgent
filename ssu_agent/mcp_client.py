from langchain_mcp_adapters.client import MultiServerMCPClient

from ssu_agent import config


def create_mcp_client() -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {
            "ssuMCP": {
                "url": config.SSUMCP_URL,
                "transport": "streamable_http",
            }
        }
    )
