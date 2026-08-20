"""`**/` means zero or more directories, the way everything else means it.

`fnmatch` has no notion of `**`: it compiles `**/*.py` to a regex requiring a
literal slash, so the pattern matched nothing at the search root. Git, ripgrep,
fd and every editor read it as *zero* or more, and it is the first form a model
reaches for.

The consequence was worse than a missed result. In a real session the agent
globbed `**/todo.py` for a file it had written at the top of the project one
turn earlier, was told "No files matching", and carried on having concluded the
file did not exist. A coding agent that cannot find a file it just wrote will
rewrite it.
"""

import pytest

from agent_server.tools.base import ToolContext
from agent_server.tools.search import _with_zero_depth, glob_search


@pytest.fixture
def project(tmp_path):
    (tmp_path / "todo.py").write_text("x = 1\n")
    (tmp_path / "notes.md").write_text("hi\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("y = 2\n")
    (tmp_path / "src" / "deep").mkdir()
    (tmp_path / "src" / "deep" / "inner.py").write_text("z = 3\n")
    return tmp_path


async def _names(project, pattern):
    result = await glob_search(ToolContext(session_id="s", project_dir=str(project)),
                               pattern=pattern)
    if "No files matching" in result.output:
        return []
    return sorted(result.output.splitlines())


# ── The expansion ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pattern,expected", [
    ("**/todo.py", {"**/todo.py", "todo.py"}),
    ("**/*.py", {"**/*.py", "*.py"}),
    ("src/**/*.py", {"src/**/*.py", "src/*.py"}),
    ("*.py", {"*.py"}),
    ("src/*.py", {"src/*.py"}),
])
def test_every_double_star_is_read_both_ways(pattern, expected):
    assert set(_with_zero_depth([pattern])) == expected


def test_two_double_stars_give_every_combination():
    assert set(_with_zero_depth(["**/a/**/*.py"])) == {
        "**/a/**/*.py", "a/**/*.py", "**/a/*.py", "a/*.py",
    }


def test_a_double_star_mid_segment_is_not_touched():
    """`x**/y` is not the `**/` idiom and must not be rewritten."""
    assert _with_zero_depth(["x**/y"]) == ["x**/y"]


# ── What it finds ────────────────────────────────────────────────────────────

async def test_a_file_at_the_root_is_found_by_double_star(project):
    assert await _names(project, "**/todo.py") == ["todo.py"]


async def test_double_star_finds_every_depth_at_once(project):
    assert await _names(project, "**/*.py") == [
        "src/app.py", "src/deep/inner.py", "todo.py",
    ]


async def test_a_scoped_double_star_still_scopes(project):
    """The fix must not make `src/**/*.py` match things outside src."""
    found = await _names(project, "src/**/*.py")
    assert found == ["src/app.py", "src/deep/inner.py"]
    assert "todo.py" not in found


async def test_a_plain_pattern_is_unchanged(project):
    assert await _names(project, "*.py") == [
        "src/app.py", "src/deep/inner.py", "todo.py",
    ]


async def test_a_pattern_that_matches_nothing_still_matches_nothing(project):
    assert await _names(project, "**/*.rs") == []


async def test_braces_and_depth_expand_together(project):
    found = await _names(project, "**/*.{py,md}")
    assert "todo.py" in found
    assert "notes.md" in found
    assert "src/app.py" in found
