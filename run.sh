#!/bin/bash
# Run the CodeAgent server
cd "$(dirname "$0")"
source .venv/bin/activate
# Kill anything on port 8080
lsof -ti:8080 | xargs -r kill -9 2>/dev/null
sleep 0.5
python -m uvicorn agent_server.main:app --host 0.0.0.0 --port 8080 --reload
