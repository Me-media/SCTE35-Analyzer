from fastapi import Request

from app.db import Database
from app.jobs.manager import JobManager


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs
