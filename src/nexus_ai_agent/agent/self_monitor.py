import psutil
import os
import time
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

class SelfMonitor:
    def __init__(self, max_ram_mb: int = 1500):
        self.max_ram_mb = max_ram_mb
        self.start_time = time.time()

    async def check_health(self) -> Dict[str, Any]:
        """Check system health status."""
        process = psutil.Process(os.getpid())
        ram_mb = process.memory_info().rss / (1024 * 1024)
        disk = psutil.disk_usage('/')
        uptime = time.time() - self.start_time
        
        status = {
            "ram_mb": ram_mb,
            "ram_percent": (ram_mb / self.max_ram_mb) * 100,
            "disk_free_gb": disk.free / (1024**3),
            "uptime_hours": uptime / 3600,
            "healthy": ram_mb < self.max_ram_mb
        }
        return status

    async def auto_fix(self, issue: str):
        """Basic auto-fix logic."""
        logger.info(f"Attempting to fix: {issue}")
        if "RAM" in issue:
            # Placeholder for model unloading or GC
            import gc
            gc.collect()
        elif "DB" in issue:
            # Placeholder for DB vacuum
            pass
