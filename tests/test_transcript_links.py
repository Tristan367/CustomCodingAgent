"""File paths in chat text, and what a tool row says about itself.

Two things this covers, both reported from real use:

  * a path containing a space was linkified as two separate links -- a drive
    called "Gaming Beast" produced one link ending at "Gaming" and another
    starting at "Beast/". The token ran to the next whitespace, and directory
    names with spaces are entirely normal on removable media.
  * a tool the user wrote showed nothing but its name while it ran, because the
    front end had a hardcoded phrasing for one particular custom tool and
    nothing at all for the rest.

The markdown module is loaded on its own into a blank page: it has no
dependencies and this keeps the regex tests fast and unambiguous.
"""

from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

REPO = Path(__file__).resolve().parent.parent
MARKDOWN_JS = REPO / "web_ui" / "static" / "js" / "markdown.js"


@pytest.fixture(scope="module")
def markdown_source():
    return MARKDOWN_JS.read_text()


@pytest.fixture
async def render(markdown_source):
    """Returns a callable: text -> the list of file links markdown makes of it."""
    async with playwright_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no Playwright browser available: {exc}")
        page = await browser.new_page()
        await page.goto("about:blank")
        await page.add_script_tag(content=markdown_source)

        async def links(text):
            return await page.evaluate(
                """(t) => {
                    const d = document.createElement('div');
                    d.innerHTML = md.render(t);
                    return [...d.querySelectorAll('a.file-ref')].map(
                        a => a.getAttribute('data-path')
                             + (a.dataset.line ? ':' + a.dataset.line : ''));
                }""",
                text,
            )

        try:
            yield links
        finally:
            await browser.close()


# ── Paths with spaces in them ────────────────────────────────────────────────

async def test_a_directory_with_a_space_stays_one_link(render):
    """The reported case, from a drive named "Gaming Beast"."""
    got = await render(
        '/run/media/tristan/OS/Users/GAMING BEAST/Pictures/encounter tables/1000008342.jpg '
        'is the photo')
    assert got == [
        "/run/media/tristan/OS/Users/GAMING BEAST/Pictures/encounter tables/1000008342.jpg"
    ]


async def test_a_final_directory_with_a_space_is_not_cut_short(render):
    """The reported message ended "…/AI-Fantasy-Images/encounter tables".

    Nothing follows to prove the path carries on, so the "does it continue"
    rule stopped at "encounter" -- one link, but pointing at a directory that
    does not exist. A last segment is taken when the path already contains a
    space and the line ends there.
    """
    got = await render(
        "and those files can be found here: "
        "/run/media/tristan/OS/Users/GAMING BEAST/Pictures/encounter tables")
    assert got == ["/run/media/tristan/OS/Users/GAMING BEAST/Pictures/encounter tables"]


async def test_a_path_without_spaces_does_not_swallow_the_next_word(render):
    """The other side of that rule: "done" is prose, not a directory."""
    assert await render("open /tmp/x done") == ["/tmp/x"]
    assert await render("the file is /tmp/report.txt") == ["/tmp/report.txt"]


async def test_a_trailing_directory_with_a_space_stays_one_link(render):
    got = await render("look in /media/tristan/Gaming Beast/ for it")
    assert got == ["/media/tristan/Gaming Beast/"]


async def test_a_filename_with_a_space_stays_one_link(render):
    got = await render("~/Documents/My Notes/todo.md and nothing else")
    assert got == ["~/Documents/My Notes/todo.md"]


async def test_the_path_stops_at_the_prose_after_it(render):
    """The other half of the same problem: a space only continues a path when
    the path visibly carries on past it."""
    assert await render("Run /usr/bin/env and then stop") == ["/usr/bin/env"]
    assert await render("open /tmp/notes.txt when you can") == ["/tmp/notes.txt"]


async def test_two_paths_separated_by_a_comma_stay_separate(render):
    """A space after sentence punctuation is never inside a path."""
    assert await render("/tmp/one.txt, /tmp/two.txt") == ["/tmp/one.txt", "/tmp/two.txt"]


async def test_prose_with_slashes_in_it_is_not_a_path(render):
    """"n/a and/or AC/DC" is three English idioms. Allowing spaces inside bare
    segment paths swallowed the lot as a single link."""
    assert await render("that is n/a and/or AC/DC") == []


async def test_the_ordinary_cases_still_work(render):
    assert await render("agent_server/routes/context.py:313 has it") == [
        "agent_server/routes/context.py:313"]
    assert await render("see ./tools/run.sh now") == ["./tools/run.sh"]
    assert await render("path is /a/b/c.py.") == ["/a/b/c.py"]
    assert await render("visit https://example.com/a b") == []


async def test_an_abbreviated_path_still_links_the_part_that_is_real(render):
    """The regression this rescanning exists for.

    "`.../encounter tables/extracted/`" is how an agent writes a shortened path.
    The match swallowed " tables/extracted/" as a trailing segment, then gave up
    because ".../encounter" is not a path -- and because the global pass had
    already moved past what it swallowed, the part that *was* a path never got
    its own turn. The result was no link at all where there had been one.
    """
    assert await render("`.../encounter tables/extracted/`") == ["tables/extracted/"]
    assert await render(".../encounter tables/extracted/") == ["tables/extracted/"]
    assert await render("saved to `.../encounter tables/extracted/` now") == [
        "tables/extracted/"]


async def test_a_path_inside_backticks_is_still_a_link(render):
    assert await render("`/tmp/report.txt`") == ["/tmp/report.txt"]
    assert await render("see `./tools/run.sh` for it") == ["./tools/run.sh"]
