"""Application configurers."""
import abc
import logging
import os
import shutil
import socket
import subprocess
from io import StringIO
from string import Template

from django.conf import settings

from git import Repo

import paramiko
from paramiko.ssh_exception import AuthenticationException
from paramiko.ssh_exception import BadHostKeyException
from paramiko.ssh_exception import SSHException

import requests

from retrying import RetryError, retry


log = logging.getLogger(__name__)

DEFAULT_INVENTORY_TEMPLATE = """
${host}

[all:vars]
ansible_ssh_port=22
ansible_user='${user}'
ansible_ssh_private_key_file=pk
ansible_ssh_extra_args='-o StrictHostKeyChecking=no'
""".strip()


def create_configurer(app_config):
    """Create a configurer based on the 'runner' in app_config."""
    # Default to ansible if no runner
    runner = app_config.get('config_appliance', {}).get('runner', 'ansible')
    if runner == "ansible":
        return AnsibleAppConfigurer()
    elif runner == "script":
        return ScriptAppConfigurer()
    else:
        raise ValueError("Unsupported value of 'runner': {}".format(runner))


class AppConfigurer():
    """Interface class for application configurer."""

    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def validate(self, app_config, provider_config):
        """Throws exception if provider_config or app_config isn't valid."""
        pass

    @abc.abstractmethod
    def configure(self, app_config, provider_config):
        """
        Configure application on already provisioned host.

        See AppPlugin.deploy for additional documentation on arguments.
        """
        pass


class SSHBasedConfigurer(AppConfigurer):

    def validate(self, app_config, provider_config):
        # Validate SSH connection info in provider_config
        host_config = provider_config.get('host_config', {})
        host = host_config.get('host_address')
        user = host_config.get('ssh_user')
        ssh_private_key = host_config.get('ssh_private_key')
        log.debug("Config ssh key:\n%s", ssh_private_key)
        try:
            self._check_ssh(host, pk=ssh_private_key, user=user)
        except RetryError as rte:
            raise Exception("Error trying to ssh to host {}: {}".format(
                host, rte))

    def _remove_known_host(self, host):
        """
        Remove a host from ~/.ssh/known_hosts.

        :type host: ``str``
        :param host: Hostname or IP address of the host to remove from the
                     known hosts file.

        :rtype: ``bool``
        :return: True if the host was successfully removed.
        """
        cmd = "ssh-keygen -R {0}".format(host)
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        (out, err) = p.communicate()
        if p.wait() == 0:
            return True
        return False

    @retry(retry_on_result=lambda result: result is False, wait_fixed=5000,
           stop_max_delay=180000)
    def _check_ssh(self, host, pk=None, user='ubuntu'):
        """
        Check for ssh availability on a host.

        :type host: ``str``
        :param host: Hostname or IP address of the host to check.

        :type pk: ``str``
        :param pk: Private portion of an ssh key.

        :type user: ``str``
        :param user: Username to use when trying to login.

        :rtype: ``bool``
        :return: True if ssh connection was successful.
        """
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = self._get_private_key_from_string(pk)
        try:
            log.info("Trying to ssh {0}@{1}".format(user, host))
            ssh.connect(host, username=user, pkey=pkey)
            self._remove_known_host(host)
            return True
        except (BadHostKeyException, AuthenticationException,
                SSHException, socket.error) as e:
            log.warn("ssh connection exception for {0}: {1}".format(host, e))
        self._remove_known_host(host)
        return False

    def _get_private_key_from_string(self, private_key):
        pkey = None
        if private_key:
            if 'RSA' not in private_key:
                # Paramiko requires key type so add it
                log.info("Augmenting private key with RSA type")
                private_key = private_key.replace(' PRIVATE', ' RSA PRIVATE')
            key_file_object = StringIO(private_key)
            pkey = paramiko.RSAKey.from_private_key(key_file_object)
            key_file_object.close()
        return pkey


class ScriptAppConfigurer(SSHBasedConfigurer):

    def validate(self, app_config, provider_config):
        super().validate(app_config, provider_config)
        config_script = app_config.get('config_appliance', {}).get(
            'config_script')
        if not config_script:
            raise Exception("config_appliance missing required parameter: "
                            "config_script")

    def configure(self, app_config, provider_config):
        host_config = provider_config.get('host_config', {})
        host = host_config.get('host_address')
        user = host_config.get('ssh_user')
        ssh_private_key = host_config.get('ssh_private_key')
        # TODO: maybe add support for running multiple commands, but how to
        # distinguish from run_cmd?
        config_script = app_config.get('config_appliance', {}).get(
            'config_script')

        try:
            pkey = self._get_private_key_from_string(ssh_private_key)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            log.info("Trying to ssh {0}@{1}".format(user, host))
            ssh.connect(host, username=user, pkey=pkey)
            stdin, stdout, stderr = ssh.exec_command(config_script)
            self._remove_known_host(host)
            return {
                'stdout': stdout.read(),
                'stderr': stderr.read()
            }
        except SSHException as sshe:
            raise Exception("Failed to execute '{}' on {}".format(
                config_script, host)) from sshe


class AnsibleAppConfigurer(SSHBasedConfigurer):

    def validate(self, app_config, provider_config):
        super().validate(app_config, provider_config)

        # validate required app_config values
        playbook = app_config.get('config_appliance', {}).get('repository')
        if not playbook:
            raise Exception("config_appliance missing required parameter: "
                            "repository")

    def configure(self, app_config, provider_config, playbook_vars=None):
        host_config = provider_config.get('host_config', {})
        host = host_config.get('host_address')
        user = host_config.get('ssh_user')
        ssh_private_key = host_config.get('ssh_private_key')
        if 'RSA' not in ssh_private_key:
            ssh_private_key = ssh_private_key.replace(
                ' PRIVATE', ' RSA PRIVATE')
            log.info("Agumented ssh key with RSA type: %s" % ssh_private_key)
        playbook = app_config.get('config_appliance', {}).get('repository')
        if 'inventoryTemplate' in app_config.get('config_appliance', {}):
            inventory = app_config.get(
                'config_appliance', {}).get('inventoryTemplate')
        else:
            inventory = DEFAULT_INVENTORY_TEMPLATE
        self._run_playbook(playbook, inventory, host, ssh_private_key, user,
                           playbook_vars)
        return {}

    def _run_playbook(self, playbook, inventory, host, pk, user='ubuntu',
                      playbook_vars=None):
        """
        Run an Ansible playbook to configure a host.

        First clone a playbook from the supplied repo if not already
        available, configure the Ansible inventory, and run the playbook.

        The method assumes ``ansible-playbook`` system command is available.

        :type playbook: ``str``
        :param playbook: A URL of a git repository where the playbook resides.

        :type inventory: ``str``
        :param inventory: A URL pointing to a string ``Template``-like file
                          that will be used for running the playbook. The
                          file should have defined variables for ``host`` and
                          ``user``.

        :type playbook_vars: ``list`` of tuples
        :param playbook_vars: A list of key/value tuples with variables to pass
                              to the playbook via command line arguments
                              (i.e., --extra-vars key=value).

        :type host: ``str``
        :param host: Hostname or IP of a machine as the playbook target.

        :type pk: ``str``
        :param pk: Private portion of an ssh key.

        :type user: ``str``
        :param user: Target host system username with which to login.
        """
        # Clone the repo in its own dir if multiple tasks run simultaneously
        # The path must be to a folder that doesn't already contain a git repo,
        # including any parent folders
        # TODO: generalize this temporary directory
        repo_path = '/tmp/cloudlaunch_plugin_runners/rancher_ansible_%s' % host
        try:
            log.info("Delete plugin runner folder %s if not empty", repo_path)
            shutil.rmtree(repo_path)
        except FileNotFoundError:
            pass
        try:
            # Ensure the playbook is available
            log.info("Cloning Ansible playbook %s to %s", playbook, repo_path)
            Repo.clone_from(playbook, to_path=repo_path)
            # Create a private ssh key file
            pkf = os.path.join(repo_path, 'pk')
            with os.fdopen(os.open(pkf, os.O_WRONLY | os.O_CREAT, 0o600),
                           'w') as f:
                f.writelines(pk)
            # Create an inventory file
            r = requests.get(inventory)
            inv = Template((r.content).decode('utf-8'))
            inventory_path = os.path.join(repo_path, 'inventory')
            with open(inventory_path, 'w') as f:
                log.info("Creating inventory file %s", inventory_path)
                f.writelines(inv.substitute({'host': host, 'user': user}))
            # Run the playbook
            cmd = ["ansible-playbook", "-i", "inventory", "playbook.yml"]
            for pev in playbook_vars or []:
                cmd += ["--extra-vars", "{0}=\"{1}\"".format(pev[0], pev[1])]
            log.info("Running Ansible with command %s", " ".join(cmd))
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                       universal_newlines=True, cwd=repo_path)
            output_buffer = ""
            while process.poll() is None:
                output = process.stdout.readline()
                output_buffer += output
                if output:
                    log.info(output)
            if process.poll() != 0:
                raise Exception("An error occurred while running the ansible playbook to"
                                "configure instance. Check the logs. Last output lines"
                                "were: %s".format(output.split("\n")[-10:]))
            log.info("Playbook status: %s", process.poll())
        finally:
            if not settings.DEBUG:
                log.info("Deleting ansible playbook %s", repo_path)
                shutil.rmtree(repo_path)
        return 0, output_buffer
