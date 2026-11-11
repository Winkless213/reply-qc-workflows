from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from qc_steps import mask_text


class MaskTextTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        masked_text, masked_count = mask_text(tool_parameters.get("text") or "")
        yield self.create_json_message({"masked_text": masked_text, "masked_count": masked_count})
        yield self.create_variable_message("masked_text", masked_text)
        yield self.create_variable_message("masked_count", masked_count)
