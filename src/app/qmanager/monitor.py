# fastapi-accutrack, Apache-2.0 license
# Filename: qmanager/monitor.py
# Description: A simple thread to monitor the job database and run queued jobs
import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import docker
from deepsea_ai.config.config import Config
from deepsea_ai.database.job import JobType, PydanticJobWithMedias, Status
from deepsea_ai.database.job.database_helper import get_status, update_media
from sqlalchemy.orm import sessionmaker

from app.conf import local_config_ini_path
from app.job import Job2
from app.logger import debug, info, err
from app.runner.local import DockerRunner, default_name


class Monitor:

    def __init__(self, session_maker: sessionmaker):
        super().__init__()
        self.session_maker = session_maker
        self.stopped = False

        # Get the configuration for the minio server
        cfg = Config(local_config_ini_path.as_posix())
        self.s3_root_bucket = cfg('minio', 's3_root_bucket')
        self.s3_track_prefix = cfg('minio', 's3_track_prefix')
        self.s3_strongsort_track_config = cfg('minio', 's3_strongsort_track_config')

    def stop(self):
        self.stopped = True

    def notify(self, email: str, status: Status):
        """
        Notify user by email when a job is complete
        :param job: The job to notify the user about
        :return:
        """
        # Send an email notification if an email address was provided
        # if email:
        #     send_notification_email(email)
        debug(f'Notifying user {email}  with status {status}')

    def run(self):
        if self.stopped:
            return

        client = docker.from_env()

        # Get all active docker containers
        all_containers = client.containers.list(all=True, filters={'name': default_name})

        info(f'Found {len(all_containers)} active {default_name} docker containers')

        with self.session_maker.begin() as db:
            docker_jobs = db.query(Job2).filter(Job2.job_type == JobType.DOCKER).all()
            info(f'Found {len(docker_jobs)} docker jobs')
            # Get all the job ids with status QUEUED and RUNNING
            jobs_ids_queued = [job.id for job in docker_jobs if get_status(job) == Status.QUEUED]
            jobs_ids_running = [job.id for job in docker_jobs if get_status(job) == Status.RUNNING]

        if len(jobs_ids_queued) > 0 and len(all_containers) == 0:
            with self.session_maker.begin() as db:
                # Get the first job in the queue
                job = db.query(Job2).filter(Job2.id == jobs_ids_queued[0]).first()

                video_url = job.media[0].name  # Docker jobs have 1 media per job, so get the first one

                # Make a prefix for the output based on the video path (sans http) and the current time
                video_s3 = Path(urlparse(video_url).path)
                key = f"{video_s3.parent}{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
                output_s3 = f"s3://{self.s3_root_bucket}/{self.s3_track_prefix}/{key}/output"

                update_media(db, job, video_url, Status.RUNNING)
                model_s3 = job.engine
                job_id = job.id

            # Create a docker runner instance and run it
            info(f'Running job {job_id} with output {output_s3}')
            instance = DockerRunner(job_id=job_id,
                                    output_s3=output_s3,
                                    video_url=video_url,
                                    model_s3=model_s3,
                                    track_s3=self.s3_strongsort_track_config,
                                    args='--iou-thres 0.5 --conf-thres 0.01 --agnostic-nms --max-det 100')

            instance.run()

            with self.session_maker.begin() as db:
                job = db.query(Job2).filter(Job2.id == jobs_ids_queued[0]).first()
                job.engine = instance.container.id
                update_media(db, job, video_url, Status.RUNNING)
                self.notify(job, Status.RUNNING)

            # Wait for the container to finish
            num_tries = 0
            while not instance.is_successful() and num_tries < 3:
                time.sleep(30)
                num_tries += 1

            with self.session_maker.begin() as db:
                if instance.is_successful():
                    info(f'Processing complete: {instance.is_successful()}')
                    update_media(db, job, video_url, Status.SUCCESS)
                    self.notify(job.email, Status.SUCCESS)
                else:
                    err(f'Processing complete: {instance.is_successful()}')
                    update_media(db, job, video_url, Status.FAILED)
                    self.notify(job.email, Status.FAILED)

            info(f'Job {job.id} finished running')

        if len(jobs_ids_running) > 0:
            for job_id in jobs_ids_running:
                # Should never get here unless something went wrong and the service was restarted
                err(f'Job {job_id} was running but the service was restarted')
                with self.session_maker.begin() as db:
                    job = db.query(Job2).filter(Job2.id == job_id).first()
                    job.media[0].status = Status.FAILED
                    # Stop the container by its id and remove it
                    container_id = job.engine
                    container = client.containers.get(container_id)
                    if container:
                        container.stop()
                        container.remove()
                    self.notify(job, Status.FAILED)
