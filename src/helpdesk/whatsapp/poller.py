import asyncio
from dataclasses import replace

import structlog

from helpdesk import clock, files, inbound
from helpdesk.whatsapp.parse import parse_notification

log = structlog.get_logger()

MAX_FAILURES = 3


async def poll_once(sf, green, files_dir: str, failures: dict[int, int], retry_sleep=1.0) -> bool:
    got = await green.receive(timeout=20)
    if got is None:
        return False
    receipt_id, body = got
    msg = parse_notification(body)
    if msg is not None:
        try:
            file_path = None
            if msg.download_url:
                try:
                    path = files.new_file_path(files_dir, msg.file_name, clock.utcnow())
                    await green.download(msg.download_url, path)
                    file_path = str(path)
                except Exception as e:
                    log.warning("wa file download failed", error=str(e))
                    msg = replace(msg, text=f"{msg.text or ''} [файл не загружен]".strip())
            async with sf() as s, s.begin():
                await inbound.handle_incoming(s, msg, file_path)
        except Exception:
            n = failures[receipt_id] = failures.get(receipt_id, 0) + 1
            log.exception("inbound message failed", receipt_id=receipt_id, attempt=n)
            if n < MAX_FAILURES:
                await asyncio.sleep(retry_sleep)
                return True  # не удаляем — Green API отдаст уведомление снова
    await green.delete(receipt_id)
    failures.pop(receipt_id, None)
    return True


async def run(sf, green, files_dir: str, stop: asyncio.Event, error_sleep: float = 5.0) -> None:
    failures: dict[int, int] = {}
    while not stop.is_set():
        try:
            await poll_once(sf, green, files_dir, failures)
        except Exception:
            log.exception("green api poll failed")
            try:
                await asyncio.wait_for(stop.wait(), error_sleep)
            except TimeoutError:
                pass
