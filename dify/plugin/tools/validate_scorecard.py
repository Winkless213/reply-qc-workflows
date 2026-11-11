from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from qc_steps import validate


class ValidateScorecardTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        p = tool_parameters
        result = validate(p.get("raw"), p.get("my_reply_count"), p.get("prompt_version"))
        yield self.create_json_message(result)
        yield self.create_variable_message("result", result)
