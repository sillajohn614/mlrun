# Copyright 2023 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import asyncio
import datetime
import typing

import sqlalchemy.orm
from fastapi.concurrency import run_in_threadpool

import mlrun.common.schemas
import mlrun.config
import mlrun.errors
import mlrun.lists
import mlrun.runtimes
import mlrun.runtimes.constants
import mlrun.utils.singleton
import server.api.api.utils
import server.api.constants
import server.api.db.session
import server.api.runtime_handlers
import server.api.utils.background_tasks
import server.api.utils.clients.log_collector
import server.api.utils.singletons.db
from mlrun.utils import logger


class Runs(
    metaclass=mlrun.utils.singleton.Singleton,
):
    def store_run(
        self,
        db_session: sqlalchemy.orm.Session,
        data: dict,
        uid: str,
        iter: int = 0,
        project: str = mlrun.mlconf.default_project,
    ):
        project = project or mlrun.mlconf.default_project

        # Some runtimes do not use the submit job flow, so their notifications are not masked.
        # Redact notification params if not concealed with a secret
        server.api.api.utils.mask_notification_params_on_task(
            data, server.api.constants.MaskOperations.REDACT
        )

        server.api.utils.singletons.db.get_db().store_run(
            db_session,
            data,
            uid,
            project,
            iter=iter,
        )

    def update_run(
        self,
        db_session: sqlalchemy.orm.Session,
        project: str,
        uid: str,
        iter: int,
        data: dict,
    ):
        project = project or mlrun.mlconf.default_project
        logger.debug("Updating run", project=project, uid=uid, iter=iter)
        # TODO: Abort run moved to a separate endpoint, remove this section once in 1.8.0
        #  (once 1.5.x clients are not supported)
        if (
            data
            and data.get("status.state") == mlrun.runtimes.constants.RunStates.aborted
        ):
            current_run = server.api.utils.singletons.db.get_db().read_run(
                db_session, uid, project, iter
            )
            if (
                current_run.get("status", {}).get("state")
                in mlrun.runtimes.constants.RunStates.terminal_states()
            ):
                raise mlrun.errors.MLRunConflictError(
                    "Run is already in terminal state, can not be aborted"
                )
            runtime_kind = current_run.get("metadata", {}).get("labels", {}).get("kind")
            if runtime_kind not in mlrun.runtimes.RuntimeKinds.abortable_runtimes():
                raise mlrun.errors.MLRunBadRequestError(
                    f"Run of kind {runtime_kind} can not be aborted"
                )
            # aborting the run meaning deleting its runtime resources
            # TODO: runtimes crud interface should ideally expose some better API that will hold inside itself the
            #  "knowledge" on the label selector
            server.api.crud.RuntimeResources().delete_runtime_resources(
                db_session,
                label_selector=f"mlrun/project={project},mlrun/uid={uid}",
                force=True,
            )
        server.api.utils.singletons.db.get_db().update_run(
            db_session, data, uid, project, iter
        )

    def get_run(
        self,
        db_session: sqlalchemy.orm.Session,
        uid: str,
        iter: int,
        project: str = mlrun.mlconf.default_project,
    ) -> dict:
        project = project or mlrun.mlconf.default_project
        return server.api.utils.singletons.db.get_db().read_run(
            db_session, uid, project, iter
        )

    def list_runs(
        self,
        db_session: sqlalchemy.orm.Session,
        name: typing.Optional[str] = None,
        uid: typing.Optional[typing.Union[str, list[str]]] = None,
        project: str = "",
        labels: typing.Optional[typing.Union[str, list[str]]] = None,
        states: typing.Optional[list[str]] = None,
        sort: bool = True,
        last: int = 0,
        iter: bool = False,
        start_time_from: datetime.datetime = None,
        start_time_to: datetime.datetime = None,
        last_update_time_from: datetime.datetime = None,
        last_update_time_to: datetime.datetime = None,
        partition_by: mlrun.common.schemas.RunPartitionByField = None,
        rows_per_partition: int = 1,
        partition_sort_by: mlrun.common.schemas.SortField = None,
        partition_order: mlrun.common.schemas.OrderType = mlrun.common.schemas.OrderType.desc,
        max_partitions: int = 0,
        requested_logs: bool = None,
        return_as_run_structs: bool = True,
        with_notifications: bool = False,
    ) -> mlrun.lists.RunList:
        project = project or mlrun.mlconf.default_project
        return server.api.utils.singletons.db.get_db().list_runs(
            session=db_session,
            name=name,
            uid=uid,
            project=project,
            labels=labels,
            states=states,
            sort=sort,
            last=last,
            iter=iter,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
            last_update_time_from=last_update_time_from,
            last_update_time_to=last_update_time_to,
            partition_by=partition_by,
            rows_per_partition=rows_per_partition,
            partition_sort_by=partition_sort_by,
            partition_order=partition_order,
            max_partitions=max_partitions,
            requested_logs=requested_logs,
            return_as_run_structs=return_as_run_structs,
            with_notifications=with_notifications,
        )

    async def delete_run(
        self,
        db_session: sqlalchemy.orm.Session,
        uid: str,
        iter: int,
        project: str = mlrun.mlconf.default_project,
    ):
        project = project or mlrun.mlconf.default_project
        try:
            run = server.api.utils.singletons.db.get_db().read_run(
                db_session, uid, project, iter
            )
        except mlrun.errors.MLRunNotFoundError:
            logger.debug(
                "Run not found, nothing to delete",
                project=project,
                uid=uid,
                iter=iter,
            )
            return

        run_state = run.get("status", {}).get("state")
        if (
            run_state
            in mlrun.runtimes.constants.RunStates.not_allowed_for_deletion_states()
        ):
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"Can not delete run in {run_state} state, consider aborting the run first"
            )

        runtime_kind = run.get("metadata", {}).get("labels", {}).get("kind")
        if runtime_kind in mlrun.runtimes.RuntimeKinds.runtime_with_handlers():
            runtime_handler = server.api.runtime_handlers.get_runtime_handler(
                runtime_kind
            )
            if runtime_handler.are_resources_coupled_to_run_object():
                runtime_handler.delete_runtime_object_resources(
                    server.api.utils.singletons.db.get_db(),
                    db_session,
                    object_id=uid,
                    label_selector=f"mlrun/project={project}",
                    force=True,
                )

        logger.debug(
            "Deleting run",
            project=project,
            uid=uid,
            iter=iter,
            runtime_kind=runtime_kind,
        )
        server.api.utils.singletons.db.get_db().del_run(db_session, uid, project, iter)

        await self._post_delete_run(project, uid)

    async def delete_runs(
        self,
        db_session: sqlalchemy.orm.Session,
        name=None,
        project: str = mlrun.mlconf.default_project,
        labels=None,
        state=None,
        days_ago: int = 0,
        runs_list: mlrun.lists.RunList = None,
    ):
        project = project or mlrun.mlconf.default_project
        if (
            state
            and state
            in mlrun.runtimes.constants.RunStates.not_allowed_for_deletion_states()
        ):
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"Can not delete runs in {state} state, consider aborting the run first"
            )

        if not runs_list:
            start_time_from = None
            if days_ago:
                start_time_from = datetime.datetime.now(
                    datetime.timezone.utc
                ) - datetime.timedelta(days=days_ago)

            runs_list = self.list_runs(
                db_session,
                name=name,
                project=project,
                labels=labels,
                states=[state] if state else None,
                start_time_from=start_time_from,
                return_as_run_structs=False,
            )

        failed_deletions = 0
        last_exception = None
        while runs_list:
            tasks = []
            for run in runs_list[
                : mlrun.config.config.crud.runs.batch_delete_runs_chunk_size
            ]:
                tasks.append(
                    server.api.db.session.run_function_with_new_db_session(
                        self.delete_run,
                        run.uid,
                        run.iteration,
                        run.project,
                    )
                )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    failed_deletions += 1
                    last_exception = result
                    run = runs_list[i]
                    logger.warning(
                        "Failed to delete run",
                        run_uid=run.uid,
                        run_name=run.name,
                        project=run.project,
                        error=mlrun.errors.err_to_str(result),
                    )

            runs_list = runs_list[
                mlrun.config.config.crud.runs.batch_delete_runs_chunk_size :
            ]

        if failed_deletions:
            raise mlrun.errors.MLRunBadRequestError(
                f"Failed to delete {failed_deletions} run(s). Error: {mlrun.errors.err_to_str(last_exception)}"
            ) from last_exception

    def abort_run(
        self,
        db_session: sqlalchemy.orm.Session,
        project: str,
        uid: str,
        iter: int = 0,
        run_updates: typing.Optional[dict] = None,
        run: typing.Optional[dict] = None,
        new_background_task_id: typing.Optional[str] = None,
    ):
        project = project or mlrun.mlconf.default_project
        run_updates = run_updates or {}
        run_updates["status.state"] = mlrun.runtimes.constants.RunStates.aborted
        logger.debug(
            "Aborting run",
            project=project,
            uid=uid,
            iter=iter,
            new_background_task_id=new_background_task_id,
        )

        if not run:
            run = server.api.utils.singletons.db.get_db().read_run(
                db_session, uid, project, iter
            )

        current_run_state = run.get("status", {}).get("state")
        # ensure we are not triggering multiple internal aborts / internal abort on top of user abort
        if (
            new_background_task_id == server.api.constants.internal_abort_task_id
            and current_run_state
            in [
                mlrun.runtimes.constants.RunStates.aborting,
                mlrun.runtimes.constants.RunStates.aborted,
            ]
        ):
            logger.warning(
                "Run is aborting/aborted, skipping internal abort",
                new_background_task_id=new_background_task_id,
                current_run_state=current_run_state,
            )
            return

        if current_run_state in mlrun.runtimes.constants.RunStates.terminal_states():
            raise mlrun.errors.MLRunConflictError(
                "Run is already in terminal state, can not be aborted"
            )

        runtime_kind = run.get("metadata", {}).get("labels", {}).get("kind")
        if runtime_kind not in mlrun.runtimes.RuntimeKinds.abortable_runtimes():
            raise mlrun.errors.MLRunBadRequestError(
                f"Run of kind {runtime_kind} can not be aborted"
            )

        # mark run as aborting
        aborting_updates = {
            "status.state": mlrun.runtimes.constants.RunStates.aborting,
            "status.abort_task_id": new_background_task_id,
        }
        server.api.utils.singletons.db.get_db().update_run(
            db_session, aborting_updates, uid, project, iter
        )

        run_updates["status.state"] = mlrun.runtimes.constants.RunStates.aborted
        try:
            # aborting the run meaning deleting its runtime resources
            # TODO: runtimes crud interface should ideally expose some better API that will hold inside itself the
            #  "knowledge" on the label selector
            server.api.crud.RuntimeResources().delete_runtime_resources(
                db_session,
                label_selector=f"mlrun/project={project},mlrun/uid={uid}",
                force=True,
            )

        except Exception as exc:
            err = mlrun.errors.err_to_str(exc)
            logger.warning(
                "Failed to abort run",
                err=err,
                project=project,
                uid=uid,
                iter=iter,
            )
            run_updates = {
                "status.state": mlrun.runtimes.constants.RunStates.error,
                "status.error": f"Failed to abort run, error: {err}",
            }
            server.api.utils.singletons.db.get_db().update_run(
                db_session, run_updates, uid, project, iter
            )
            raise exc

        server.api.utils.singletons.db.get_db().update_run(
            db_session, run_updates, uid, project, iter
        )

    @staticmethod
    async def _post_delete_run(project, uid):
        if (
            mlrun.mlconf.log_collector.mode
            != mlrun.common.schemas.LogsCollectorMode.legacy
        ):
            await server.api.crud.Logs().delete_run_logs(project, uid)
        else:
            await run_in_threadpool(
                server.api.crud.Logs().delete_run_logs_legacy,
                project,
                uid,
            )
