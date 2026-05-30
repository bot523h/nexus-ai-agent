import asyncio
import os
from nexus_ai_agent.features.story_gen import AIStoryGenerator

async def main():
    gen = AIStoryGenerator()
    text = "سلام دنیا! این یک تست برای پشتیبانی از زبان فارسی است."
    output = "test_story.png"
    await gen.generate_story_image(text, output)
    if os.path.exists(output):
        print(f"Success: {output} created.")
    else:
        print("Failed to create image.")

if __name__ == "__main__":
    asyncio.run(main())
