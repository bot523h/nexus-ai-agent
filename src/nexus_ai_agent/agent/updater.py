import logging
import subprocess

import httpx

logger = logging.getLogger(__name__)


class AutoUpdater:
    def __init__(self, current_version: str, repo: str = "bot523h/nexus-ai-agent") -> None:
        self.current_version = current_version
        self.repo = repo

    async def check_for_update(self) -> tuple[bool, str | None]:
        """Check GitHub for latest release."""
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    latest = resp.json().get("tag_name")
                    if latest != self.current_version:
                        return True, latest
            except Exception as e:
                logger.error(f"Update check error: {e}")
        return False, None

    async def do_update(self) -> bool:
        """Perform git pull and reinstall."""
        try:
            subprocess.run(["git", "pull"], check=True)
            subprocess.run(["pip", "install", "-r", "requirements.txt"], check=True)
            logger.info("Update completed. Please restart the bot.")
            return True
        except Exception as e:
            logger.error(f"Update failed: {e}")
            return False
