"""测试用的外部 MCP 服务（stdio）：一个只读工具、一个破坏性工具、一个普通写入工具"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("shop")
ORDERS = {"A1": "shipped", "B2": "pending"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def lookup(order_id: str) -> dict:
    """查询订单状态"""
    if order_id not in ORDERS:
        raise ValueError(f"订单不存在: {order_id}")
    return {"id": order_id, "status": ORDERS[order_id]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def cancel(order_id: str) -> str:
    """取消订单"""
    ORDERS[order_id] = "cancelled"
    return "cancelled"


@mcp.tool()
def add_note(order_id: str, note: str) -> str:
    """给订单加备注"""
    return "ok"


if __name__ == "__main__":
    mcp.run()
