#!/usr/bin/env bash
# A minimal custom tool: echoes its single argument back to the model.
#
# Install it on the Tools page (Tools -> New Tool) with:
#
#   Name:        echo
#   Description: Echo the given message back.
#   Parameters:
#     {"type":"object",
#      "properties":{"msg":{"type":"string"}},
#      "required":["msg"]}
#
# How custom tools work: the model's arguments arrive as environment variables
# named TOOL_ARG_<NAME>, and whatever the script prints to stdout is returned
# to the model as the tool result. This one is deliberately trivial so the
# format is obvious; a real tool is just a longer script.
echo "$TOOL_ARG_MSG"
