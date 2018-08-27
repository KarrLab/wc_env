""" Tools for managing computing environments for whole-cell modeling

* Build the Docker image
* Remove the Docker image
* Push/pull the Docker image
* Create Docker containers

    1. Mount host directories into container
    2. Copy files (such as configuration files and authentication keys into container
    3. Install GitHub SSH key
    4. Verify access to GitHub
    5. Install Python packages from PyPI
    6. Install Python package from GitHub (e.g. WC models and WC modeling tools)
    7. Install Python packages in mounted directories from host

* Run models/tools in Docker containers
    
    * Run the WC command line utility inside Docker containers, including with a 
      mixture of code installed from GitHub and mounted from the host
    * Run Python sessions in Docker containers, including running code on the host
      mounted to the container
    * Test Python code on host by mounting the code to a Docker container and using pytest
      to test the code inside the container

* Copy files to/from Docker container
* List Docker containers of the image
* Get CPU, memory, network usage statistics of Docker containers
* Stop Docker containers
* Remove Docker containers
* Login to DockerHub

:Author: Jonathan Karr <jonrkarr@gmail.com>
:Author: Arthur Goldberg <Arthur.Goldberg@mssm.edu>
:Date: 2018-08-23
:Copyright: 2018, Karr Lab
:License: MIT
"""

from datetime import datetime
from pathlib import Path
import configparser
import dateutil.parser
import docker
import enum
import fnmatch
import io
import json
import os
import re
import requests
import shutil
import subprocess
import tempfile
import wc_env_manager.config.core
import yaml


class WcEnvUser(enum.Enum):
    """ WC environment users and their ids """
    root = 0
    container_user = 999


class WcEnvManager(object):
    """ Manage computing environments (Docker containers) for whole-cell modeling

    Attributes:
        base_docker_image_repo (:obj:`str`): name of base Docker image for environment
        base_docker_image_tags (:obj:`list` of :obj:`str`): list of tags for base Docker 
            repository
        docker_image_repo (:obj:`str`): name of Docker image for environment
        docker_image_tags (:obj:`list` of :obj:`str`): list of tags for Docker 
            repository
        dockerfile_path (:obj:`str`): path to Dockerfile for environment
        docker_image_build_args (:obj:`dict`): build argument for Dockerfile
        docker_image_context_path (:obj:`str`): path to context to build Docker 
            image
        docker_container_name_format (:obj:`str`): format for timestamped names of 
            Docker containers generated by :obj:`datetime.datetime.strftime`
        paths_to_copy_to_docker_container (:obj:`dict`): dictionary of paths to copy 
            to Docker container
        paths_to_mount_to_docker_container (:obj:`dict`): dictionary of paths to mount 
            to Docker container
        ssh_key_container_path (:obj:`str`): path to passphrase-less SSH key for GitHub 
            within container
        python_version_in_container (:obj:`str`): version of Python to use in Docker 
            container
        python_packages_from_pypi (:obj:`str`): list of Python packages to install from 
            PyPI in requirements.txt format
        python_packages_from_github (:obj:`str`): list of Python packages to install from 
            GitHub in requirements.txt format
        python_packages_from_host (:obj:`str`): list of Python packages to install from 
            host in requirements.txt format
        dockerhub_username (:obj:`str`): username for DockerHub
        dockerhub_password (:obj:`str`): password for DockerHub
        verbose (:obj:`bool`): if :obj:`True`, print status messages to stdout

        _docker_client (:obj:`docker.client.DockerClient`): client connected to the Docker daemon
        _docker_image (:obj:`docker.models.images.Image`): current Docker image
        _docker_container (:obj:`docker.models.containers.Container`): current Docker container
    """

    # todo: reduce privileges in Docker image by creating separate user
    # todo: manipulate Python path for packages without setup.py

    def __init__(self,
                 base_docker_image_repo=None, base_docker_image_tags=None,
                 docker_image_repo=None, docker_image_tags=None,
                 dockerfile_path=None, docker_image_build_args=None, docker_image_context_path=None,
                 docker_container_name_format=None,
                 paths_to_copy_to_docker_container=None,
                 paths_to_mount_to_docker_container=None,
                 ssh_key_container_path=None,
                 python_version_in_container=None,
                 python_packages_from_pypi=None,
                 python_packages_from_github=None,
                 python_packages_from_host=None,
                 dockerhub_username=None, dockerhub_password=None,
                 verbose=None):
        """
        Args:
            base_docker_image_repo (:obj:`str`, optional): name of base Docker repository for 
                environment
            base_docker_image_tags (:obj:`list` of :obj:`str`, optional): list of tags for 
                base Docker repository
            docker_image_repo (:obj:`str`, optional): name of Docker repository for 
                environment
            docker_image_tags (:obj:`list` of :obj:`str`, optional): list of tags for 
                Docker repository
            dockerfile_path (:obj:`str`, optional): path to Dockerfile for environment
            docker_image_build_args (:obj:`dict`, optional): build argument for Dockerfile
            docker_image_context_path (:obj:`str`, optional): path to context to build 
                Docker image
            docker_container_name_format (:obj:`str`, optional): format for timestamped 
                names of Docker containers generated by :obj:`datetime.datetime.strftime`
            paths_to_copy_to_docker_container (:obj:`dict`, optional): dictionary of 
                paths to copy to Docker container
            paths_to_mount_to_docker_container (:obj:`dict`, optional): dictionary of 
                paths to mount to Docker container
            ssh_key_container_path (:obj:`str`, optional): path to passphrase-less SSH 
                key for GitHub within container
            python_version_in_container (:obj:`str`, optional): version of Python to use 
                in Docker container
            python_packages_from_pypi (:obj:`str`, optional): list of Python packages to 
                install from PyPI in requirements.txt format
            python_packages_from_github (:obj:`str`, optional): list of Python packages to 
                install from GitHub in requirements.txt format
            python_packages_from_host (:obj:`str`, optional): list of Python packages to 
                install from host in requirements.txt format
            dockerhub_username (:obj:`str`, optional): username for DockerHub
            dockerhub_password (:obj:`str`, optional): password for DockerHub
            verbose (:obj:`bool`, optional): if :obj:`True`, print status messages to stdout
        """

        # get default configuration
        default_config = wc_env_manager.config.core.get_config()['wc_env_manager']

        # handle options
        for key, default_val in default_config.items():
            val = eval(key)
            if val is None:
                val = default_val
            setattr(self, key, val)

        # load Docker client
        self._docker_client = docker.from_env()

        # get image and current container
        self.set_docker_image(self.get_latest_docker_image(self.base_docker_image_repo))
        self.set_docker_container(self.get_latest_docker_container())

    def build_base_docker_image(self):
        """ Build base Docker image for WC modeling environment

        Returns:
            :obj:`docker.models.images.Image`: Docker image

        Raises:
            :obj:`WcEnvManagerError`: if image context is not a directory or
                there is an error building the image
        """
        # build image
        if self.verbose:
            print('Building base image {} with tags {{{}}} ...'.format(
                self.base_docker_image_repo, ', '.join(self.base_docker_image_tags)))

        if not os.path.isdir(self.docker_image_context_path):
            raise WcEnvManagerError('Docker image context "{}" must be a directory'.format(
                self.docker_image_context_path))

        with open(self.dockerfile_path, 'rb') as dockerfile:
            try:
                image, log = self._docker_client.images.build(
                    path=self.docker_image_context_path,
                    fileobj=dockerfile,
                    pull=True,
                    buildargs=self.docker_image_build_args,
                )
            except requests.exceptions.ConnectionError as exception:
                raise WcEnvManagerError("Docker connection error: service must be running:\n  {}".format(
                    str(exception).replace('\n', '\n  ')))
            except docker.errors.APIError as exception:
                raise WcEnvManagerError("Docker API error: Dockerfile contains syntax errors:\n  {}".format(
                    str(exception).replace('\n', '\n  ')))
            except docker.errors.BuildError as exception:
                raise WcEnvManagerError("Docker build error: Error building Dockerfile:\n  {}".format(
                    str(exception).replace('\n', '\n  ')))
            except Exception as exception:
                raise WcEnvManagerError("{}:\n  {}".format(
                    exception.__class__.__name__, str(exception).replace('\n', '\n  ')))

        # tag image
        for tag in self.base_docker_image_tags:
            image.tag(self.base_docker_image_repo, tag=tag)

        # re-get image because tags don't automatically update on image object
        image = self._docker_client.images.get('{}:{}'.format(self.base_docker_image_repo, self.base_docker_image_tags[0]))

        # print log
        if self.verbose:
            for entry in log:
                if 'stream' in entry:
                    print(entry['stream'], end='')
                elif 'id' in entry and 'status' in entry:
                    print('{}: {}'.format(entry['id'], entry['status']))
                else:
                    pass

        # store reference to latest image
        self._docker_image = image

        return image

    def remove_docker_image(self, image_repo, image_tags, force=False):
        """ Remove version of Docker image

        Args:
            image_repo (:obj:`str`): image repository
            image_tags (:obj:`list` of :obj:`str`): list of tags
            force (:obj:`bool`, optional): if :obj:`True`, force removal of the version of the
                image (e.g. even if a container with the image is running)
        """
        for tag in image_tags:
            self._docker_client.images.remove('{}:{}'.format(image_repo, tag), force=True)

    def login_dockerhub(self):
        """ Login to DockerHub """
        self._docker_client.login(self.dockerhub_username, password=self.dockerhub_password)

    def push_docker_image(self, image_repo, image_tags):
        """ Push Docker image to DockerHub 
        
        Args:
            image_repo (:obj:`str`): image repository
            image_tags (:obj:`list` of :obj:`str`): list of tags
        """
        for tag in image_tags:
            self._docker_client.images.push(image_repo, tag)

    def pull_docker_image(self, image_repo, image_tags):
        """ Pull Docker image for WC modeling environment

        Args:
            image_repo (:obj:`str`): image repository
            image_tags (:obj:`list` of :obj:`str`): list of tags

        Returns:
            :obj:`docker.models.images.Image`: Docker image
        """
        self._docker_image = self._docker_client.images.pull(image_repo, tag=image_tags[0])
        return self._docker_image

    def set_docker_image(self, image):
        """ Set the Docker image for WC modeling environment

        Args:
            image (:obj:`docker.models.images.Image` or :obj:`str`): Docker image
                or name of Docker image
        """
        if isinstance(image, str):
            image = self._docker_client.images.get(image)
        self._docker_image = image

    def get_latest_docker_image(self, image_repo):
        """ Get the lastest version of the Docker image for the WC modeling environment

        Args:
            image_repo (:obj:`str`): image repository

        Returns:
            :obj:`docker.models.images.Image`: Docker image
        """
        try:
            return self._docker_client.images.get(image_repo)
        except docker.errors.ImageNotFound:
            return None

    def get_docker_image_version(self):
        """ Get the version of the Docker image

        Returns:
            :obj:`str`: docker image version
        """
        for tag in self._docker_image.tags:
            _, _, version = tag.partition(':')
            if re.match(r'^\d+\.\d+\.\d+[a-zA-Z0-9]*$', version):
                return version

    def create_docker_container(self, tty=True):
        """ Create Docker container for WC modeling environmet

        Args:
            tty (:obj:`bool`): if :obj:`True`, allocate a pseudo-TTY

        Returns:
            :obj:`docker.models.containers.Container`: Docker container
        """
        name = self.make_docker_container_name()
        container = self._docker_container = self._docker_client.containers.run(
            self.base_docker_image_repo, name=name,
            volumes=self.paths_to_mount_to_docker_container,
            stdin_open=True, tty=tty,
            detach=True,
            user=WcEnvUser.root.name)
        return container

    def make_docker_container_name(self):
        """ Create a timestamped name for a Docker container

        Returns:
            :obj:`str`: container name
        """
        return datetime.now().strftime(self.docker_container_name_format)

    def setup_docker_container(self, upgrade=False):
        container = self._docker_container

        # copy default configuration files to Docker container
        self.copy_config_files_to_docker_container(overwrite=upgrade)

        # copy additional files to Docker container
        for path in self.paths_to_copy_to_docker_container.values():
            self.copy_path_to_docker_container(path['host'], path['container'], overwrite=upgrade)

        # install SSH key
        self.install_github_ssh_host_in_docker_container(upgrade=upgrade)
        assert(self.test_github_ssh_access_in_docker_container())

        # install packages
        self.install_python_packages_in_docker_container(self.python_packages_from_pypi, upgrade=upgrade)
        self.install_python_packages_in_docker_container(self.python_packages_from_github, upgrade=upgrade, process_dependency_links=True)
        self.install_python_packages_in_docker_container(self.python_packages_from_host, upgrade=upgrade, process_dependency_links=True)

    def copy_config_files_to_docker_container(self, overwrite=False):
        """ Install configuration files from ~/.wc to Docker container 

        Args:
            overwrite (:obj:`bool`, optional): if :obj:`True`, overwrite files
        """
        container_user_dirname, _ = self.run_process_in_docker_container('bash -c "realpath ~"',
                                                                         container_user=WcEnvUser.root)
        container_config_dirname = os.path.join(container_user_dirname, '.wc')
        host_config_dirname = os.path.expanduser(os.path.join('~', '.wc'))

        if os.path.isdir(host_config_dirname):
            # copy config files from host to container
            self.copy_path_to_docker_container(host_config_dirname, container_config_dirname, 
                container_user=WcEnvUser.root, overwrite=overwrite)

            # install third party config files in container
            filename = os.path.join(host_config_dirname, 'third_party', 'paths.yml')
            with open(filename, 'r') as file:
                paths = yaml.load(file)

            for rel_src, abs_dest in paths.items():
                if abs_dest[0:2] == '~/':
                    abs_dest = os.path.join(container_user_dirname, abs_dest[2:])
                abs_dest_dir = os.path.dirname(abs_dest)
                self.run_process_in_docker_container(['mkdir', '-p', abs_dest_dir],
                                                     container_user=WcEnvUser.root)
                abs_src = os.path.join(host_config_dirname, 'third_party', rel_src)
                self.copy_path_to_docker_container(abs_src, abs_dest, container_user=WcEnvUser.root, overwrite=overwrite)

    def copy_path_to_docker_container(self, local_path, container_path, overwrite=True, container_user=WcEnvUser.root):
        """ Copy file or directory to Docker container

        Implemented using subprocess because docker-py does not (as 2018-08-22)
        provide a copy method.

        Args:
            local_path (:obj:`str`): path to local file/directory to copy to container
            container_path (:obj:`str`): path to copy file/directory within container
            overwrite (:obj:`bool`, optional): if :obj:`True`, overwrite file

        Raises:
            :obj:`WcEnvManagerError`: if the container_path already exists and 
                :obj:`overwrite` is :obj:`False`
        """
        is_path, _ = self.run_process_in_docker_container(
            'bash -c "if [ -f {0} ] || [ -d {0} ]; then echo 1; fi"'.format(container_path),
            container_user=container_user)
        if is_path and not overwrite:
            raise WcEnvManagerError('File {} already exists'.format(container_path))
        self.run_process_on_host([
            'docker', 'cp',
            local_path,
            self._docker_container.name + ':' + container_path,
        ])

    def copy_path_from_docker_container(self, container_path, local_path, overwrite=True):
        """ Copy file/directory from Docker container

        Implemented using subprocess because docker-py does not (as 2018-08-22)
        provide a copy method.

        Args:
            container_path (:obj:`str`): path to file/directory within container
            local_path (:obj:`str`): local path to copy file/directory from container
            overwrite (:obj:`bool`, optional): if :obj:`True`, overwrite file

        Raises:
            :obj:`WcEnvManagerError`: if the container_path already exists and 
                :obj:`overwrite` is :obj:`False`
        """
        is_file = os.path.isfile(local_path) or os.path.isdir(local_path)
        if is_file and not overwrite:
            raise WcEnvManagerError('File {} already exists'.format(local_path))
        self.run_process_on_host([
            'docker', 'cp',
            self._docker_container.name + ':' + container_path,
            local_path,
        ])

    def install_github_ssh_host_in_docker_container(self, upgrade=False):
        """ Install GitHub SSH host in Docker container 
        
        Args:
            upgrade (:obj:`bool`, optional): if :obj:`True`, upgrade known_hosts
        """
        self.run_process_in_docker_container('bash -c "touch ~/.ssh/known_hosts"',
                                             container_user=WcEnvUser.root)
        if upgrade:
            self.run_process_in_docker_container('bash -c "sed -i \'/github.com ssh-rsa/d\' ~/.ssh/known_hosts"',
                                                 container_user=WcEnvUser.root)
        self.run_process_in_docker_container('bash -c "ssh-keyscan github.com >> ~/.ssh/known_hosts"',
                                             container_user=WcEnvUser.root)

    def test_github_ssh_access_in_docker_container(self):
        """ Test that the Docker container has SSH access to GitHub

        Returns:
            :obj:`bool`: :obj:`True`, if Docker container has SSH access to GitHub
        """
        message, exit_code = self.run_process_in_docker_container('ssh -T git@github.com',
                                                                  check=False, container_user=WcEnvUser.root)
        return exit_code == 1 and re.search(r'successfully authenticated', message) is not None

    def install_python_packages_in_docker_container(self, packages, upgrade=False, process_dependency_links=False):
        """ Install Python packages

        Args:
            packages (:obj:`str`): list of packages in requirements.txt format
            upgrade (:obj:`bool`, optional): if :obj:`True`, upgrade package
            process_dependency_links (:obj:`bool`, optional): if :obj:`True`, install packages from provided
                URLs
        """
        # save requirements to temporary file on host
        file, host_temp_filename = tempfile.mkstemp(suffix='.txt')
        os.write(file, packages.encode('utf-8'))
        os.close(file)

        # copy requirements to temporary file in container
        container_temp_filename, _ = self.run_process_in_docker_container('mktemp', container_user=WcEnvUser.root)
        self.copy_path_to_docker_container(host_temp_filename, container_temp_filename)

        # install requirements
        cmd = ['pip{}'.format(self.python_version_in_container), 'install', '-r', container_temp_filename]
        if upgrade:
            cmd.append('-U')
        if process_dependency_links:
            cmd.append('--process-dependency-links')
        self.run_process_in_docker_container(cmd, container_user=WcEnvUser.root)

        # remove temporary files
        os.remove(host_temp_filename)
        self.run_process_in_docker_container(['rm', container_temp_filename], container_user=WcEnvUser.root)

    def convert_host_to_container_path(self, host_path):
        """ Get the corresponding container path for a host path

        Args:
            host_path (:obj:`str`): path on host

        Returns:
            :obj:`str`: corresponding path in container

        Raises:
            :obj:`WcEnvManagerError`: if the host path is not mounted
                onto the container
        """
        host_path = os.path.abspath(host_path)
        for host_mount_path, container_mount_attrs in self.paths_to_mount_to_docker_container.items():
            host_mount_path = os.path.abspath(host_mount_path)
            container_mount_path = container_mount_attrs['bind']
            if host_path.startswith(host_mount_path):
                relpath = os.path.relpath(host_path, host_mount_path)
                return os.path.join(container_mount_path, relpath)
        raise WcEnvManagerError('{} is not mounted into the container'.format(
            host_path))

    def convert_container_to_host_path(self, container_path):
        """ Get the corresponding host path for a container path

        Args:
            container_path (:obj:`str`): path in container

        Returns:
            :obj:`str`: corresponding path in host

        Raises:
            :obj:`WcEnvManagerError`: if the container path is not mounted
                from the host
        """
        for host_mount_path, container_mount_attrs in self.paths_to_mount_to_docker_container.items():
            container_mount_path = container_mount_attrs['bind']
            if container_path.startswith(container_mount_path):
                relpath = os.path.relpath(container_path, container_mount_path)
                return os.path.join(host_mount_path, relpath)
        raise WcEnvManagerError('{} is not mounted into the container'.format(
            container_path))

    def set_docker_container(self, container):
        """ Set the Docker containaer

        Args:
            container (:obj:`docker.models.containers.Container` or :obj:`str`): Docker container
                or name of Docker container
        """
        if isinstance(container, str):
            container = self._docker_client.containers.get(container)
        self._docker_container = container

    def get_latest_docker_container(self):
        """ Get current Docker container

        Returns:
            :obj:`docker.models.containers.Container`: Docker container
        """
        containers = self.get_docker_containers(sort_by_read_time=True)
        if containers:
            return containers[0]
        else:
            return None

    def get_docker_containers(self, sort_by_read_time=False):
        """ Get list of Docker containers that are WC modeling environments

        Args:
            sort_by_read_time (:obj:`bool`): if :obj:`True`, sort by read time in descending order
                (latest first)

        Returns:
            :obj:`list` of :obj:`docker.models.containers.Container`: list of Docker containers
                that are WC modeling environments
        """
        containers = []
        for container in self._docker_client.containers.list(all=True):
            try:
                datetime.strptime(container.name, self.docker_container_name_format)
                containers.append(container)
            except ValueError:
                pass

        if sort_by_read_time:
            containers.sort(reverse=True, key=lambda container: dateutil.parser.parse(container.stats(stream=False)['read']))

        return containers

    def run_process_in_docker_container(self, cmd, work_dir=None, env=None, check=True,
                                        container_user=WcEnvUser.root):
        """ Run a process in the current Docker container

        Args:
            cmd (:obj:`list` of :obj:`str` or :obj:`str`): command to run
            work_dir (:obj:`str`, optional): path to working directory within container
            env (:obj:`dict`, optional): key/value pairs of environment variables
            check (:obj:`bool`, optional): if :obj:`True`, raise exception if exit code is not 0
            container_user (:obj:`WcEnvUser`, optional): user to run commands in container

        Returns:
            :obj:`str`: output of the process

        Raises:
            :obj:`WcEnvManagerError`: if the command is not executed successfully
        """
        if not env:
            env = {}

        # execute command
        result = self._docker_container.exec_run(
            cmd, workdir=work_dir, environment=env, user=container_user.name)

        # print output
        if self.verbose:
            print(result.output.decode('utf-8')[0:-1])

        # check for errors
        if check and result.exit_code != 0:
            if not work_dir:
                result2 = self._docker_container.exec_run('pwd', user=container_user.name)
                work_dir = result2.output.decode('utf-8')[0:-1]
            raise WcEnvManagerError(
                ('Command not successfully executed in Docker container:\n'
                 '  command: {}\n'
                 '  working directory: {}\n'
                 '  environment:\n    {}\n'
                 '  exit code: {}\n'
                 '  output: {}').format(
                    cmd, work_dir,
                    '\n    '.join('{}: {}'.format(key, val) for key, val in env.items()),
                    result.exit_code,
                    result.output.decode('utf-8')))

        return (result.output.decode('utf-8')[0:-1], result.exit_code)

    def get_docker_container_stats(self):
        """ Get statistics about the CPU, io, memory, network performance of the Docker container

        Returns:
            :obj:`dict`: statistics about the CPU, io, memory, network performance of the Docker container
        """
        return self._docker_container.stats(stream=False)

    def stop_docker_container(self):
        """ Remove current Docker container """
        self._docker_container.stop()

    def remove_docker_container(self, force=False):
        """ Remove current Docker container

        Args:
            force (:obj:`bool`, optional): if :obj:`True`, force removal of the container
                (e.g. remove container even if it is running)
        """
        self._docker_container.remove(force=force)
        self._docker_container = None

    def remove_docker_containers(self, force=False):
        """ Remove Docker all containers that are WC modeling environments

        Args:
            force (:obj:`bool`, optional): if :obj:`True`, force removal of the container
                (e.g. remove containers even if they are running)
        """
        for container in self.get_docker_containers():
            container.remove(force=force)
        self._docker_container = None

    def run_process_on_host(self, cmd):
        """ Run a process on the host

        Args:
            cmd (:obj:`list` of :obj:`str` or :obj:`str`): command to run
        """
        if self.verbose:
            stdout = None
            stderr = None
        else:
            stdout = subprocess.PIPE
            stderr = subprocess.PIPE

        subprocess.run(cmd, stdout=stdout, stderr=stderr, check=True)


class WcEnvManagerError(Exception):
    """ Base class for exceptions in `wc_env_manager`

    Attributes:
        message (:obj:`str`): the exception's message
    """

    def __init__(self, message=None):
        super().__init__(message)
