"""A running command's output reaches the transcript before it finishes.

The design constraint is that this must cost effectively nothing, because a
command that floods its pipes is exactly the one you least want to slow down.
Two properties carry that, and both are pinned here: frames are paced by a clock
rather than by chunk arrivals, so the rate does not grow with output volume; and
each frame holds a bounded tail, so producing one does not cost O(total output).
"""

import asyncio
import time

import pytest

from agent_server.tools.base import ToolContext
from agent_server.tools.bash import (
    STREAM_INTERVAL_SEC,
    STREAM_TAIL_BYTES,
    _Tail,
    run_bash,
)


def _ctx(**kw) -> ToolContext:
    return ToolContext(session_id="s", project_dir="/tmp", **kw)


def _watching() -> tuple[ToolContext, asyncio.Queue]:
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    return _ctx(call_id="c1", progress=queue), queue


def _drain(queue: asyncio.Queue) -> list[str]:
    frames = []
    while not queue.empty():
        _, text = queue.get_nowait()
        frames.append(text)
    return frames


# ── The tail buffer ──────────────────────────────────────────────────────────

def test_the_tail_keeps_only_the_end():
    tail = _Tail(limit=100)
    for i in range(1000):
        tail.add(f"line {i}\n".encode())
    text = tail.text()
    assert len(text) <= 100
    assert text.endswith("line 999\n")


def test_the_tail_does_not_grow_with_what_passes_through_it():
    """The reason this is a class and not a `b"".join(sink)[-n:]`.

    Re-joining the capture sink would make each frame cost O(total output), so a
    long-running noisy command would get more expensive the longer it ran.
    """
    tail = _Tail(limit=1000)
    for _ in range(10_000):
        tail.add(b"x" * 500)
    # 5MB went through; what is retained is bounded by the limit, not by that.
    assert tail._size <= 1000 * 2
    assert len(tail.text()) <= 1000


def test_the_tail_reports_whether_anything_arrived():
    """The ticker skips a frame when nothing changed, so an idle command sends
    nothing at all rather than the same bytes ten times a second."""
    tail = _Tail()
    assert not tail.dirty
    tail.add(b"hello")
    assert tail.dirty
    tail.text()
    assert not tail.dirty


def test_the_tail_survives_a_split_multibyte_character():
    """Chunk boundaries fall wherever the pipe says, not on character
    boundaries, and a decode error mid-run would kill the whole command."""
    tail = _Tail(limit=100)
    encoded = "héllo wörld ✓".encode()
    for i in range(len(encoded)):
        tail.add(encoded[i:i + 1])
    assert "wörld" in tail.text()


# ── What actually reaches the transcript ─────────────────────────────────────

async def test_output_arrives_while_the_command_is_still_running():
    ctx, queue = _watching()
    task = asyncio.create_task(
        run_bash(ctx, command="for i in 1 2 3 4 5; do echo tick $i; sleep 0.1; done"))
    await asyncio.sleep(0.35)
    early = _drain(queue)
    assert early, "nothing streamed while the command was running"
    assert "tick" in early[-1]
    result = await task
    assert "tick 5" in result.output


async def test_the_last_frame_is_flushed_when_the_command_ends():
    """Without the final flush the transcript ends on whatever the last tick
    happened to catch, which is never the last line."""
    ctx, queue = _watching()
    result = await run_bash(ctx, command="echo first; echo last")
    frames = _drain(queue)
    assert frames, "no frame at all"
    assert "last" in frames[-1]
    assert "last" in result.output


async def test_nothing_streams_when_nobody_is_watching():
    """A subagent's bash has no transcript of its own and must pay nothing."""
    ctx = _ctx()
    assert ctx.progress is None
    result = await run_bash(ctx, command="echo quiet")
    assert "quiet" in result.output


async def test_a_full_queue_drops_frames_instead_of_blocking():
    """A browser that cannot keep up must never stall the command. Frames carry
    the whole tail, so a dropped one is a skipped repaint, not lost output."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    ctx = _ctx(call_id="c1", progress=queue)
    began = time.monotonic()
    result = await run_bash(
        ctx, command="for i in $(seq 1 40); do echo line $i; sleep 0.02; done")
    elapsed = time.monotonic() - began
    # Nobody ever drained the queue; the command still ran at its own pace.
    assert "line 40" in result.output
    assert elapsed < 5, f"a full queue slowed the command down ({elapsed:.1f}s)"


async def test_the_frame_rate_is_paced_by_time_not_by_output():
    """The property that makes this cheap: 200k lines and 10 lines cost the
    same number of frames, because both are bounded by how long the command
    ran, not by how much it said."""
    ctx, queue = _watching()
    await run_bash(ctx, command="for i in $(seq 1 200000); do echo line $i; done")
    frames = _drain(queue)
    assert frames, "a command that printed 200k lines streamed nothing"
    assert len(frames) < 40, f"{len(frames)} frames is not paced by the clock"
    assert all(len(f) <= STREAM_TAIL_BYTES for f in frames)


async def test_a_flood_is_bounded_by_the_tail_not_by_its_size():
    ctx, queue = _watching()
    result = await run_bash(ctx, command="yes hello | head -n 200000")
    frames = _drain(queue)
    assert all(len(f) <= STREAM_TAIL_BYTES for f in frames)
    assert "hello" in result.output


async def test_frames_carry_the_call_they_belong_to():
    """A batch runs its calls concurrently onto one queue, so an untagged frame
    would be rendered into whichever block happened to be last."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    await asyncio.gather(
        run_bash(_ctx(call_id="alpha", progress=queue), command="echo A"),
        run_bash(_ctx(call_id="beta", progress=queue), command="echo B"),
    )
    seen = {}
    while not queue.empty():
        call_id, text = queue.get_nowait()
        seen.setdefault(call_id, []).append(text)
    assert set(seen) == {"alpha", "beta"}
    assert "A" in seen["alpha"][-1]
    assert "B" in seen["beta"][-1]


async def test_stderr_is_streamed_too():
    ctx, queue = _watching()
    await run_bash(ctx, command="echo bad 1>&2")
    frames = _drain(queue)
    assert frames and "bad" in frames[-1]


async def test_streaming_does_not_change_what_the_model_receives():
    """The frames are display only. Whatever the transcript showed, the result
    handed to the model must be byte-identical to the unstreamed run."""
    plain = await run_bash(_ctx(), command="seq 1 500")
    ctx, _queue = _watching()
    streamed = await run_bash(ctx, command="seq 1 500")
    assert streamed.output == plain.output
    assert streamed.is_error == plain.is_error


@pytest.mark.parametrize("value", [STREAM_INTERVAL_SEC, STREAM_TAIL_BYTES])
def test_the_pacing_constants_are_sane(value):
    assert value > 0
