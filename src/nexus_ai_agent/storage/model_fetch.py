from __future__ import annotations

from pathlib import Path

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.ai_storage import AIStorageManager, ProviderConfig, StorageError

log = get_logger(__name__)


def _build_storage(settings: Settings) -> AIStorageManager:
    cache_dir = Path(getattr(settings, "cache_dir", "data/cache"))
    cfg = ProviderConfig(
        github_token=getattr(settings, "github_token", None),
        github_repo=getattr(settings, "github_repo", None),
        mega_email=getattr(settings, "mega_email", None),
        mega_password=getattr(settings, "mega_password", None),
        huggingface_token=getattr(settings, "huggingface_token", None),
        rclone_remote=getattr(settings, "rclone_remote", None),
    )
    return AIStorageManager(cache_dir=cache_dir, config=cfg)


def _default_remote_key(settings: Settings, model_name: str, version: str) -> str:
    filename = Path(settings.model_path).name
    # Stable storage key usable across providers.
    if model_name and version:
        return f"models/{model_name}/{version}/{filename}"
    if model_name:
        return f"models/{model_name}/{filename}"
    return f"models/{filename}"


def _default_hf_key(settings: Settings, model_name: str, version: str) -> str | None:
    filename = Path(settings.model_path).name
    if model_name.startswith("hf://"):
        return model_name
    # If model_name looks like a HuggingFace repo id (org/name), construct an hf:// key.
    if "/" in model_name:
        rev = version or "main"
        return f"hf://{model_name}@{rev}/{filename}"
    return None


async def ensure_model_available(settings: Settings, model_name: str, version: str) -> str:
    """
    Ensure the GGUF model at settings.model_path exists locally.

    Behavior (in order):
      1) local file exists -> return
      2) cache/provider download via unified storage manager
         - tries cache, then MEGA, then GitHub Releases, then rclone (optional), then Hugging Face
      3) return local path
    """
    dest = Path(settings.model_path)
    if dest.exists():
        return str(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    storage = _build_storage(settings)

    remote_key = _default_remote_key(settings, model_name=model_name, version=version)
    hf_key = _default_hf_key(settings, model_name=model_name, version=version)

    log.info("model_missing_attempt_fetch", model_path=str(dest), key=remote_key)
    try:
        await storage.download(remote_key=remote_key, local_path=dest)
        return str(dest)
    except StorageError as e:
        if not hf_key:
            raise
        log.info("model_fetch_fallback_hf", reason=str(e))
        await storage.download(remote_key=hf_key, local_path=dest)
        return str(dest)
