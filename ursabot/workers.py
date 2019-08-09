# Copyright 2019 RStudio, Inc.
# All rights reserved.
#
# Use of this source code is governed by a BSD 2-Clause
# license that can be found in the LICENSE_BSD file.
#
# This file contains function or sections of code that are marked as being
# derivative works of Buildbot. The above license only applies to code that
# is not marked as such.

import itertools
from io import BytesIO
from contextlib import contextmanager

import toolz
from twisted.internet import threads
from buildbot.plugins import util
from buildbot.util.logger import Logger
from buildbot.interfaces import LatentWorkerCannotSubstantiate
from buildbot.interfaces import LatentWorkerFailedToSubstantiate
from buildbot.worker.latent import States, AbstractLatentWorker
from buildbot.worker.docker import (DockerLatentWorker, _handle_stream_line,
                                    docker_py_version, docker)

from .utils import Collection, ensure_deferred

__all__ = [
    'DockerLatentWorker',
    'docker_worker_from',
    'load_workers_from',
    'docker_workers_for',
]

log = Logger()


class WorkerMixin:

    def __init__(self, *args, arch=None, tags=tuple(), **kwargs):
        """Bookkeep a bit of metadata to describe the workers"""
        self.arch = arch
        self.tags = tuple(tags)
        super().__init__(*args, **kwargs)


class DockerLatentWorker(WorkerMixin, DockerLatentWorker):

    def checkConfig(self, name, password, docker_host, image=None,
                    command=None, volumes=None, hostconfig=None, **kwargs):
        # Bypass the validation implemented in the parent class.
        if image is None:
            image = util.Property('docker_image', default=image)
        super().checkConfig(
            name, password, docker_host, image=image, command=command,
            volumes=volumes, hostconfig=hostconfig, **kwargs
        )

    @ensure_deferred
    async def reconfigService(self, name, password, docker_host, image=None,
                              command=None, volumes=None, hostconfig=None,
                              **kwargs):
        # Set the default password to None so random one is generated.
        # Let the DockerBuilder instances to lazily extend the docker volumes
        # and hostconfig via the reserved docker_volumes and docker_hostconfig
        # properties. The volumes are concatenated and the hostconfigs are
        # merged. The image is overridden.
        image = util.Property('docker_image', default=image)
        volumes = util.Transform(
            lambda a, b: list(toolz.concat([a, b])),
            volumes or [],
            util.Property('docker_volumes', [])
        )
        hostconfig = util.Transform(
            toolz.merge,
            hostconfig or {},
            util.Property('docker_hostconfig', default={})
        )
        return await super().reconfigService(
            name, password, docker_host, image=image, command=command,
            volumes=volumes, hostconfig=hostconfig, **kwargs
        )

    @ensure_deferred
    async def stopService(self):
        # XXX: _insubstantiation_notifier is unset, probably left out from
        #     a previous buildbot refactoring, so removed the check and use
        #     the start_stop_lock instead.
        #
        # License note:
        #    copied from the original implementation with minor modification
        #    to pass runtime configuration to the containers

        # the worker might be insubstantiating from buildWaitTimeout
        if self.state in [States.INSUBSTANTIATING,
                          States.INSUBSTANTIATING_SUBSTANTIATING]:
            # await self._insubstantiation_notifier.wait()
            pass

        if self.conn is not None or self.state in [States.SUBSTANTIATING,
                                                   States.SUBSTANTIATED]:
            await self._soft_disconnect(stopping_service=True)
        self._clearBuildWaitTimer()

        return await super(AbstractLatentWorker, self).stopService()

    def renderWorkerProps(self, build):
        # License note:
        #    copied from the original implementation with minor modification
        #    to pass runtime configuration to the containers
        return build.render(
            (self.image, self.dockerfile, self.hostconfig, self.volumes)
        )

    @contextmanager
    def docker_client(self):
        # Note that this is a blocking function, use it from threads
        client = self._getDockerClient()
        try:
            yield client
        except Exception as e:
            url = self.client_args['base_url']
            exc = RuntimeError(f'Worker {self} cannot connect to the docker '
                               f'daemon on host {url}')
            raise exc from e
        finally:
            client.close()

    def attach_interactive_shell(self, shell='/bin/bash'):
        # Note that this is blocking the event loop, but it's fine because it
        # is used for debugging purposes from the CLI.
        import dockerpty

        instance_id, image = self.instance['Id'][:12], self.instance['image']
        log.info(f"Attaching an interactive shell '{shell}' to container with "
                 f"id '{instance_id}' and image '{image}'")

        with self.docker_client() as client:
            dockerpty.exec_command(
                client=client,
                container=self.instance,
                command=shell,
                interactive=True
            )

    @ensure_deferred
    async def start_instance(self, build):
        # License note:
        #    copied from the original implementation with minor modification
        #    to pass runtime configuration to the containers
        if self.instance is not None:
            raise ValueError('instance active')
        args = await self.renderWorkerPropsOnStart(build)
        return await threads.deferToThread(self._thd_start_instance, *args)

    def _thd_start_instance(self, image, dockerfile, hostconfig, volumes):
        # License note:
        #    copied from the original implementation with minor modification
        #    to pass runtime configuration to the containers
        with self.docker_client() as docker_client:
            container_name = self.getContainerName()
            # cleanup the old instances
            instances = docker_client.containers(
                all=1,
                filters=dict(name=container_name))
            container_name = '/{0}'.format(container_name)
            for instance in instances:
                if container_name not in instance['Names']:
                    continue
                try:
                    docker_client.remove_container(instance['Id'], v=True,
                                                   force=True)
                except docker.errors.NotFound:
                    pass  # that's a race condition

            found = False
            if image is not None:
                found = self._image_exists(docker_client, image)
            else:
                worker_id = id(self)
                worker_name = self.workername
                image = f'{worker_name}_{worker_id}_image'
            if (not found) and (dockerfile is not None):
                log.info(f'Image {image} not found, building it from scratch')
                for line in docker_client.build(
                    fileobj=BytesIO(dockerfile.encode('utf-8')),
                    tag=image
                ):
                    for streamline in _handle_stream_line(line):
                        log.info(streamline)

            imageExists = self._image_exists(docker_client, image)
            if ((not imageExists) or self.alwaysPull) and self.autopull:
                if (not imageExists):
                    log.info(f'Image {image} not found, pulling from registry')
                docker_client.pull(image)

            if (not self._image_exists(docker_client, image)):
                log.info(f'Image {image} not found')
                raise LatentWorkerCannotSubstantiate(
                    f'Image {image} not found on docker host.'
                )

            volumes, binds = self._thd_parse_volumes(volumes)

            hostconfig['binds'] = binds
            if docker_py_version >= 2.2:
                hostconfig['init'] = True

            instance = docker_client.create_container(
                image,
                self.command,
                name=self.getContainerName(),
                volumes=volumes,
                environment=self.createEnvironment(),
                host_config=docker_client.create_host_config(
                    **hostconfig
                )
            )

            if instance.get('Id') is None:
                log.info('Failed to create the container')
                raise LatentWorkerFailedToSubstantiate(
                    'Failed to start container'
                )
            shortid = instance['Id'][:6]
            log.info(f'Container created, Id: {shortid}...')

            instance['image'] = image
            self.instance = instance
            docker_client.start(instance)
            log.info('Container started')
            if self.followStartupLogs:
                logs = docker_client.attach(
                    container=instance, stdout=True, stderr=True, stream=True)
                for line in logs:
                    line = line.strip()
                    log.info(f'docker VM {shortid}: {line}')
                    if self.conn:
                        break
                del logs

        return [instance['Id'], image]

    def _thd_stop_instance(self, instance, fast):
        with self.docker_client() as docker_client:
            log.info('Stopping container %s...' % instance['Id'][:6])
            docker_client.stop(instance['Id'])
            if not fast:
                docker_client.wait(instance['Id'])
            docker_client.remove_container(instance['Id'], v=True, force=True)
            if self.image is None:
                try:
                    docker_client.remove_image(image=instance['image'])
                except docker.errors.APIError as e:
                    log.info('Error while removing the image: %s', e)


def docker_worker_from(worker_dict, docker_host='unix://var/run/docker.sock',
                       masterFQDN=None, auto_pull=True, always_pull=False,
                       volumes=None, hostconfig=None, missing_timeout=120):
    """A thin helper function to reduce worker configuration boilerplate.

    Parameters
    ----------
    worker_dict: Dict
        Mandatory keys:
            - name (use alphanumeric and hyphen)
            - arch (any of amd64, arm64v8, arm32v7)
        Optional keys:
            - tags: List[str], default []
            - ncpus: int, default None
            - max_builds: int, default 1
            - volumes: list, default []
            - missing_timeout: int, default to the missing_timeout argument
            - auto_pull: bool, defaults to the auto_pull argument
            - always_pull: bool, defaults to the always_pull argument
            - docker_host: str, defaults to the docker_host argument
            - masterFQDN: str, defaults to the masterFQDN argument
            - hostconfig: dict, defaults to the hostconfig argument
    docker_host: str, default unix://var/run/docker.sock
    masterFQDN: str, default None
        Address of the master the worker should connect to. This value is
        passed to the docker image via environment variable BUILDMASTER.
        Note: Use 'host.docker.internal' on Docker for Mac.
    auto_pull: bool, default True
        Automatically pulls image if requested image is not on docker host.
    always_pull: bool, default False
        Always pulls image if autopull is set to True.
    volumes: List[str], default []
        List of volumes which should be attached to the docker container.
    hostconfig: dict, default {'network_mode': 'host'}
        Additional docker configurations, directly passed to the low-level
        docker APIClient.create_host_config.
        For more see https://docker-py.readthedocs.io/en/stable/api.html.
    missing_timeout: int, default 120
        Timeout for the worker preparation. In case of docker builders it is
        the time required to pull the docker image and spin up the container.
    Return
    ------
    docker_worker: DockerLatentWorker
    """
    w = worker_dict

    volumes = volumes or []
    hostconfig = {'network_mode': 'host'}

    return DockerLatentWorker(
        w['name'],
        password=None,  # auto generated
        arch=w['arch'],
        tags=w.get('tags', []),
        max_builds=w.get('max_builds', 1),
        properties={'ncpus': w.get('ncpus')},
        autopull=w.get('auto_pull', auto_pull),
        alwaysPull=w.get('always_pull', always_pull),
        docker_host=w.get('docker_host', docker_host),
        masterFQDN=w.get('masterFQDN', masterFQDN),
        volumes=w.get('volumes', volumes),
        hostconfig=w.get('hostconfig', hostconfig),
        missing_timeout=w.get('missing_timeout', missing_timeout)
    )


def load_workers_from(config_path, **kwargs):
    from ruamel.yaml import YAML

    yaml = YAML()
    with config_path.open('r') as fp:
        worker_dicts = yaml.load(fp)['workers']

    return Collection([docker_worker_from(w, **kwargs) for w in worker_dicts])


_worker_id = itertools.count()


def docker_workers_for(archs, **kwargs):
    workers = Collection()
    for wid, arch in zip(_worker_id, archs):
        worker_dict = dict(name=f'local-docker-{wid}', arch=arch)
        worker = docker_worker_from(worker_dict, **kwargs)
        workers.append(worker)
    return workers
