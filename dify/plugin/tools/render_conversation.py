from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from qc_steps import render


class RenderConversationTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        p = tool_parameters
        out = render(p.get("customer_name"), p.get("note"), p.get("messages"), p.get("bottom_lines"), p.get("knowledge"))
        yield self.create_json_message(out)
        for name, value in out.items():
            yield self.create_variable_message(name, value)
