"""In-process scheduler: no Redis or separate worker to run.

Every minute: autopilot outreach (sends or drafts texts as customers come due)
and stalled-order nudges. Every hour: relearn every customer's profile.
"""

import asyncio
import logging
import time

from bruno import cadence, outreach
from bruno.db import session_scope
from bruno.deps import get_deps
from bruno.timeutil import utcnow

log = logging.getLogger(__name__)
TICK_SECONDS = 60
RELEARN_SECONDS = 3600


def tick(state: dict) -> dict:
    deps = get_deps()
    now = utcnow()
    result: dict = {}
    with session_scope() as session:
        if time.time() - state.get("relearned", 0) > RELEARN_SECONDS:
            result["relearned"] = cadence.recompute_all(session, deps.settings, now)
            state["relearned"] = time.time()
        result.update(outreach.autopilot_tick(session, deps, now))
        result["nudged"] = outreach.nudge_stalled(session, deps, now)
    return result


async def run_forever() -> None:
    state: dict = {}
    while True:
        try:
            result = await asyncio.to_thread(tick, state)
            if any(v for k, v in result.items() if k != "relearned"):
                log.info("scheduler: %s", result)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
