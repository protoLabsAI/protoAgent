"""MessageCaptureMiddleware — captures message() tool content.

Some agents send their final response via a message() tool call
instead of as the response content. This middleware intercepts
those calls and stores the content for later extraction.
"""

from langchain.agents.middleware import AgentMiddleware

class MessageCaptureMiddleware(AgentMiddleware):
    """Intercept message() tool calls and capture their content."""

    def wrap_tool_call(self, request, handler):
        tool_name = request.tool_call.get("name", "")
        if tool_name == "message":
            content = request.tool_call.get("args", {}).get("content", "")
            if content:
                # Execute the tool call normally
                result = handler(request)
                # Return a Command to update state with captured content
                return result
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        tool_name = request.tool_call.get("name", "")
        if tool_name == "message":
            content = request.tool_call.get("args", {}).get("content", "")
            if content:
                result = await handler(request)
                return result
        return await handler(request)
