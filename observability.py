import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import update
from sqlalchemy.orm import Session
from models import ScrapeRun, RunStatus

logger = logging.getLogger(__name__)

async def run_watchdog(session, job_type: str, max_duration_hours: int = 8):
    """
    Mark any RUNNING runs for this job_type that are older than max_duration_hours as FAILED.
    """
    try:
        cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(hours=max_duration_hours)
        upd = (
            update(ScrapeRun)
            .where(ScrapeRun.job_type == job_type)
            .where(ScrapeRun.status == RunStatus.RUNNING)
            .where(ScrapeRun.started_at < cutoff)
            .values(
                status=RunStatus.FAILED,
                finished_at=datetime.now(ZoneInfo("UTC")),
                error_summary="orphaned — no completion signal received, likely killed or crashed"
            )
        )
        await session.execute(upd)
        await session.commit()
    except Exception as e:
        logger.error(f"Watchdog failed for {job_type}: {e}", exc_info=True)


class RunLogger:
    """
    A structured logger that writes a JSON line per processed item.
    """
    def __init__(self, job_type: str, platform: str, run_id: str, started_at: datetime):
        self.job_type = job_type
        self.platform = platform or "all"
        self.run_id = run_id
        # Use a safe ISO format without colons for Windows paths
        time_str = started_at.isoformat().replace(":", "").replace("+", "Z")
        os.makedirs("logs", exist_ok=True)
        self.filepath = f"logs/{self.job_type}_{self.platform}_{self.run_id}_{time_str}.log"
        self._file = open(self.filepath, "a", encoding="utf-8")

    def log_item(self, item_data: dict):
        try:
            self._file.write(json.dumps(item_data) + "\n")
            self._file.flush()
        except Exception as e:
            logger.error(f"Failed to write to run log: {e}", exc_info=True)

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass
