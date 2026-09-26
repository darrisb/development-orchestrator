"""SQLAlchemy-backed LangGraph checkpoints (build.md section 27).

The application tables remain the source of truth for runs, reviews and
events.  These rows only remember the graph cursor and its small serializable
state so an invocation can continue after the process that started it exits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import WorkflowCheckpointRow, WorkflowWriteRow


class SqlAlchemyCheckpointSaver(BaseCheckpointSaver[int]):
    """A compact checkpointer using the orchestrator's existing database.

    Each operation owns a short transaction. This is important: graph state
    must survive independently of the business node's transaction, including
    a process exit immediately after LangGraph records a completed node.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        super().__init__()
        self.session_factory = session_factory

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        namespace = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = get_checkpoint_id(config)
        with self.session_factory() as session:
            statement = select(WorkflowCheckpointRow).where(
                WorkflowCheckpointRow.thread_id == thread_id,
                WorkflowCheckpointRow.checkpoint_ns == namespace,
            )
            if checkpoint_id:
                statement = statement.where(
                    WorkflowCheckpointRow.checkpoint_id == checkpoint_id
                )
            else:
                statement = statement.order_by(
                    WorkflowCheckpointRow.checkpoint_id.desc()
                ).limit(1)
            row = session.scalar(statement)
            return self._tuple(session, row) if row else None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        with self.session_factory() as session:
            statement = select(WorkflowCheckpointRow)
            if config:
                configurable = config["configurable"]
                statement = statement.where(
                    WorkflowCheckpointRow.thread_id
                    == str(configurable["thread_id"])
                )
                if "checkpoint_ns" in configurable:
                    statement = statement.where(
                        WorkflowCheckpointRow.checkpoint_ns
                        == str(configurable.get("checkpoint_ns", ""))
                    )
                if checkpoint_id := get_checkpoint_id(config):
                    statement = statement.where(
                        WorkflowCheckpointRow.checkpoint_id == checkpoint_id
                    )
            if before and (before_id := get_checkpoint_id(before)):
                statement = statement.where(
                    WorkflowCheckpointRow.checkpoint_id < before_id
                )
            statement = statement.order_by(
                WorkflowCheckpointRow.checkpoint_id.desc()
            )
            if limit is not None:
                statement = statement.limit(max(0, limit))
            rows = list(session.scalars(statement))
            tuples = [self._tuple(session, row) for row in rows]
        for value in tuples:
            if filter and not all(value.metadata.get(k) == v for k, v in filter.items()):
                continue
            yield value

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, int],
    ) -> RunnableConfig:
        del new_versions
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        namespace = str(configurable.get("checkpoint_ns", ""))
        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        key = (thread_id, namespace, checkpoint["id"])
        with self.session_factory.begin() as session:
            row = session.get(WorkflowCheckpointRow, key)
            values = {
                "parent_checkpoint_id": configurable.get("checkpoint_id"),
                "checkpoint_type": checkpoint_type,
                "checkpoint": checkpoint_bytes,
                "metadata_type": metadata_type,
                "checkpoint_metadata": metadata_bytes,
            }
            if row is None:
                session.add(
                    WorkflowCheckpointRow(
                        thread_id=thread_id,
                        checkpoint_ns=namespace,
                        checkpoint_id=checkpoint["id"],
                        **values,
                    )
                )
            else:
                for key_name, value in values.items():
                    setattr(row, key_name, value)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        namespace = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = str(configurable["checkpoint_id"])
        with self.session_factory.begin() as session:
            for position, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, position)
                key = (thread_id, namespace, checkpoint_id, task_id, index)
                row = session.get(WorkflowWriteRow, key)
                if row is not None and index >= 0:
                    continue
                value_type, value_bytes = self.serde.dumps_typed(value)
                if row is None:
                    session.add(
                        WorkflowWriteRow(
                            thread_id=thread_id,
                            checkpoint_ns=namespace,
                            checkpoint_id=checkpoint_id,
                            task_id=task_id,
                            idx=index,
                            channel=channel,
                            value_type=value_type,
                            value=value_bytes,
                            task_path=task_path,
                        )
                    )
                else:
                    row.channel = channel
                    row.value_type = value_type
                    row.value = value_bytes
                    row.task_path = task_path

    def delete_thread(self, thread_id: str) -> None:
        with self.session_factory.begin() as session:
            session.execute(
                delete(WorkflowWriteRow).where(
                    WorkflowWriteRow.thread_id == thread_id
                )
            )
            session.execute(
                delete(WorkflowCheckpointRow).where(
                    WorkflowCheckpointRow.thread_id == thread_id
                )
            )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for value in self.list(config, filter=filter, before=before, limit=limit):
            yield value

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, int],
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)

    def _tuple(
        self, session: Session, row: WorkflowCheckpointRow
    ) -> CheckpointTuple:
        writes = list(
            session.scalars(
                select(WorkflowWriteRow)
                .where(
                    WorkflowWriteRow.thread_id == row.thread_id,
                    WorkflowWriteRow.checkpoint_ns == row.checkpoint_ns,
                    WorkflowWriteRow.checkpoint_id == row.checkpoint_id,
                )
                .order_by(WorkflowWriteRow.task_id, WorkflowWriteRow.idx)
            )
        )
        config: RunnableConfig = {
            "configurable": {
                "thread_id": row.thread_id,
                "checkpoint_ns": row.checkpoint_ns,
                "checkpoint_id": row.checkpoint_id,
            }
        }
        parent = (
            {
                "configurable": {
                    "thread_id": row.thread_id,
                    "checkpoint_ns": row.checkpoint_ns,
                    "checkpoint_id": row.parent_checkpoint_id,
                }
            }
            if row.parent_checkpoint_id
            else None
        )
        return CheckpointTuple(
            config=config,
            checkpoint=self.serde.loads_typed((row.checkpoint_type, row.checkpoint)),
            metadata=self.serde.loads_typed(
                (row.metadata_type, row.checkpoint_metadata)
            ),
            parent_config=parent,
            pending_writes=[
                (
                    write.task_id,
                    write.channel,
                    self.serde.loads_typed((write.value_type, write.value)),
                )
                for write in writes
            ],
        )


__all__ = ["SqlAlchemyCheckpointSaver"]
