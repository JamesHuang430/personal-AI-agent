"""Opt-in live acceptance run: exactly one 4-second 768P director project.

Run inside the API container with its existing settings. This spends provider credit.
An explicit --create and a new state file are required to create a project. Re-running
an existing state resumes observation/confirmation, never creates another project.
The temporary session is scoped to --user-id and revoked on exit; no token is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select

from assistant_app.core.config import get_settings
from assistant_app.core.security import new_session_token
from assistant_app.db.models import User
from assistant_app.db.runtime import RuntimeDependencies


def emit(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


def save(path, data):
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def check_trial(project):
    if (
        project["target_seconds"] != 4
        or project["resolution"] != "768P"
        or not project["one_click"]
        or project["planned_shots"] != 1
    ):
        raise ValueError("Stopped: project exceeds the authorized one-clip 4s/768P trial")
    if project["status"] == "awaiting_storyboard" and len(project.get("storyboard", [])) != 1:
        raise ValueError("Stopped: storyboard is not exactly one shot")


def prepare_state(args):
    state_path = Path(args.state_file)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("user_id") != str(args.user_id) or not state.get("project_id"):
            raise ValueError(
                "Ambiguous prior creation or different owner; inspect state before retrying"
            )
    elif args.create:
        if not args.premise:
            raise ValueError("--premise is required for creation")
        # Exclusive file creation protects against two operators creating the same trial.
        with state_path.open("x", encoding="utf-8") as file:
            state = {"user_id": str(args.user_id), "project_id": None, "phase": "creating"}
            json.dump(state, file)
        os.chmod(state_path, 0o600)
    else:
        raise ValueError("No saved run; pass --create explicitly to authorize a new paid trial")
    return state_path, state


async def main(args):
    state_path, state = await asyncio.to_thread(prepare_state, args)
    settings = get_settings()
    runtime = RuntimeDependencies(settings)
    token, digest = new_session_token()
    session_key = f"session:user:{digest}"
    try:
        async with runtime.sessions() as session:
            user = await session.scalar(select(User).where(User.id == args.user_id, User.is_active))
            if user is None:
                raise ValueError("Active user not found")
        await runtime.redis.set(session_key, str(args.user_id), ex=3600)
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000/api/v1",
            cookies={"assistant_session": token},
            timeout=90,
        ) as client:

            async def request(method, path, **kwargs):
                response = await client.request(method, path, **kwargs)
                if response.is_error:
                    raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
                return response.json()

            if state["project_id"] is None:
                project = await request(
                    "POST",
                    "/director/projects",
                    json={
                        "premise": args.premise,
                        "target_seconds": 4,
                        "resolution": "768P",
                        "aspect_ratio": "9:16",
                        "one_click": True,
                        "story_confirmed": True,
                    },
                )
                state["project_id"] = project["id"]
                state["phase"] = "planning"
                await asyncio.to_thread(save, state_path, state)
                emit(project_id=project["id"], phase="planning")
            project_id = state["project_id"]
            if args.resume:
                project = await request("GET", f"/director/projects/{project_id}")
                check_trial(project)
                await request("POST", f"/director/projects/{project_id}/resume")
            if args.feedback_file:
                feedback = json.loads(
                    await asyncio.to_thread(Path(args.feedback_file).read_text, encoding="utf-8")
                )
                project = await request(
                    "PUT", f"/director/projects/{project_id}/feedback", json=feedback
                )
                state["feedback"] = project["feedback"]
                await asyncio.to_thread(save, state_path, state)
                emit(
                    project_id=project_id,
                    feedback_saved=True,
                    remember=feedback.get("remember", False),
                )
                return
            previous = None
            async with asyncio.timeout(1800):
                while True:
                    project = await request("GET", f"/director/projects/{project_id}")
                    check_trial(project)
                    marker = (project["status"], project["current_stage"], project["progress"])
                    if marker != previous:
                        emit(
                            project_id=project_id,
                            status=marker[0],
                            stage=marker[1],
                            progress=marker[2],
                        )
                        previous = marker
                    state["project"] = project
                    await asyncio.to_thread(save, state_path, state)
                    if project["status"] == "failed":
                        emit(error=project["error_message"])
                        raise RuntimeError("Trial failed; no automatic paid regeneration")
                    if project["status"] == "awaiting_storyboard":
                        emit(
                            storyboard=project["storyboard"],
                            memories_used=len(
                                project.get("personalization", {}).get("memories", [])
                            ),
                        )
                        if not args.approve:
                            emit(phase="awaiting_operator_review")
                            return
                        project = await request(
                            "POST",
                            f"/director/projects/{project_id}/approve-storyboard",
                            json={"storyboard_hash": project["storyboard_hash"]},
                        )
                        state["phase"] = "rendering"
                        await asyncio.to_thread(save, state_path, state)
                    if project["status"] == "completed":
                        for shot in project["shots"]:
                            job = shot.get("video") or {}
                            if job.get("seconds") != "4" or job.get("resolution") != "768P":
                                raise ValueError(
                                    "Generated job parameters exceeded trial constraints"
                                )
                        if len(project["shots"]) != 1 or not project["quality_report"].get(
                            "passed"
                        ):
                            raise ValueError("Trial failed output validation")
                        state["phase"] = "awaiting_user_feedback"
                        await asyncio.to_thread(save, state_path, state)
                        emit(
                            phase=state["phase"],
                            final_video=project["final_video"],
                            quality=project["quality_report"],
                        )
                        return
                    await asyncio.sleep(5)
    finally:
        await runtime.redis.delete(session_key)
        await runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=UUID)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument(
        "--resume", action="store_true", help="Explicitly resume a reviewed failure"
    )
    parser.add_argument("--premise")
    parser.add_argument("--feedback-file")
    asyncio.run(main(parser.parse_args()))
