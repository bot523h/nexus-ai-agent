"""Persian story rendering is a regression test, not a root-output debug script."""

from pathlib import Path

from PIL import Image

from nexus_ai_agent.features.story_gen import AIStoryGenerator


async def test_persian_story_renders_into_private_test_workspace(tmp_path: Path) -> None:
    output = tmp_path / "story.png"
    await AIStoryGenerator().generate_story_image(
        "سلام دنیا! این یک تست برای پشتیبانی از زبان فارسی است.", str(output)
    )
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.size == (1080, 1920)
        image.verify()
