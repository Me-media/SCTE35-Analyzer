"""Live event stream for one job: SCTE-35 markers, raw cues, and saved
segments, pushed to the browser as they happen via app.jobs.manager's
on_event -> _broadcast bridge. On connect, replays a backlog of everything
recorded so far so a client that opens (or reloads) the page mid-job isn't
staring at a blank table until the next live event arrives.
"""
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("scte35_analyzer.ws")
router = APIRouter()


@router.websocket("/ws/jobs/{job_id}")
async def job_events_ws(websocket: WebSocket, job_id: str):
    await websocket.accept()
    db = websocket.app.state.db
    jobs = websocket.app.state.jobs

    job = db.get_job(job_id)
    if job is None:
        await websocket.close(code=4404)
        return

    jobs.subscribe(job_id, websocket)
    try:
        if job.get("pid_info"):
            await websocket.send_text(json.dumps(
                {"kind": "pid_info", "data": job["pid_info"], "backlog": True}, default=str))
        for marker in db.list_markers(job_id, since_id=0, limit=5000):
            await websocket.send_text(json.dumps(
                {"kind": "match_result", "data": marker, "backlog": True}, default=str))
        for seg in db.list_segments(job_id, since_id=0, limit=500):
            await websocket.send_text(json.dumps(
                {"kind": "segment_saved", "data": seg, "backlog": True}, default=str))
        for cs in db.list_cue_snapshots(job_id, since_id=0, limit=1000):
            await websocket.send_text(json.dumps(
                {"kind": "reference_snapshot_saved", "data": cs, "backlog": True}, default=str))
        await websocket.send_text(json.dumps(
            {"kind": "status", "data": {"status": job["status"], "message": job.get("error")},
             "backlog": True}, default=str))

        while True:
            # No messages are expected from the client -- this just keeps
            # the coroutine parked so a disconnect is detected promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("WebSocket error for job %s", job_id)
    finally:
        jobs.unsubscribe(job_id, websocket)
