import subprocess
import asyncio
import os

BASH_TIMEOUT = 120  # seconds


async def run_bash(*, command: str, timeout: int | None = None, workdir: str | None = None) -> str:
    timeout_sec = (timeout / 1000) if timeout else BASH_TIMEOUT
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir or os.getcwd(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        result = stdout.decode("utf-8", errors="replace")
        if stderr:
            result += "\n[stderr]\n" + stderr.decode("utf-8", errors="replace")
        result = result.strip() or f"Command completed with exit code {proc.returncode}"
        return result
    except asyncio.TimeoutError:
        return f"Command timed out after {timeout_sec}s"
    except Exception as e:
        return f"Error executing command: {e}"
