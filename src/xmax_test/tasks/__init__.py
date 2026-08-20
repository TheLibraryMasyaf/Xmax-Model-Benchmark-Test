"""Frozen task allocation and one-task-at-a-time workers."""

from .service import TaskAllocator, TaskWorker

__all__ = ["TaskAllocator", "TaskWorker"]
