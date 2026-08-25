"""Sample Zero plugin (GAP 7): registers an `echo_upper` tool.

Install by copying into ``$ZERO_HOME/plugins/`` (user) or
``/opt/zero/plugins/`` (system), then restart the app. The tool becomes
grantable and invocable through the standard capability pipeline.
"""


def register(manage_context):
    """Contract entry point: receive ManageContext, register extensions."""

    def echo_upper_handler(input_data, context):
        text = str(input_data.get("text", ""))
        return {"echoed": text.upper(), "length": len(text)}

    manage_context.tool_registry.register_tool(
        name="echo_upper",
        description="Echo the input text in upper case (sample plugin).",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "echoed": {"type": "string"},
                "length": {"type": "integer"},
            },
        },
        handler_key="plugin:echo_upper",
        handler=echo_upper_handler,
        inline=True,
    )
