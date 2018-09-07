###################################################################
# Copyright 2013-2015 All Rights Reserved
# Authors: The Paradrop Team
###################################################################

"""
Functions associated with deploying and cleaning up docker containers.
"""

import docker
import json
import os
import random
import re
import six
import subprocess
import time

from twisted.internet.threads import deferToThread

from paradrop.base.exceptions import ChuteNotFound
from paradrop.base.output import out
from paradrop.base import constants, nexus, settings
from paradrop.core.config.devices import resetWirelessDevice

from .chutecontainer import ChuteContainer
from .dockerfile import Dockerfile


DOCKER_CONF = """
# Docker systemd configuration
#
# This configuration file was automatically generated by Paradrop.  Any changes
# will be overwritten on startup.

# Tell docker not to start containers automatically on startup.
DOCKER_OPTIONS="--restart=false"
"""

# Used to match and suppress noisy progress messages from Docker output.
#
# Example:
# Extracting
# 862a3e9af0ae
# [================================================>  ] 64.06 MB/65.7 MB
suppress_re = re.compile("^(Downloading|Extracting|[a-z0-9]+|\[=*>?\s*\].*)$")


def getPortList(chute):
    """
    Get a list of ports to expose in the format expected by create_container.

    Uses the port binding dictionary from the chute host_config section.
    The keys are expected to be integers or strings in one of the
    following formats: "port" or "port/protocol".

    Example:
    port_bindings = {
        "1111/udp": 1111,
        "2222": 2222
    }
    getPortList returns [(1111, 'udp'), (2222, 'tcp')]
    """
    if not hasattr(chute, 'host_config') or chute.host_config == None:
        config = {}
    else:
        config = chute.host_config

    ports = []
    for port in config.get('port_bindings', {}).keys():
        if isinstance(port, int):
            ports.append((port, 'tcp'))
            continue

        parts = port.split('/')
        if len(parts) == 1:
            ports.append((int(parts[0]), 'tcp'))
        else:
            ports.append((int(parts[0]), parts[1]))

    # If the chute is configured to host a web service, check
    # whether there is already an entry in the list for the
    # web port.  If not, we should add one.
    web_port = chute.getWebPort()
    if web_port is not None:
        if not any(p[0] == web_port for p in ports):
            ports.append((web_port, 'tcp'))

    return ports


def writeDockerConfig():
    """
    Write options to Docker configuration.

    Mainly, we want to tell Docker not to start containers automatically on
    system boot.
    """
    # First we have to find the configuration file.
    # On ubuntu 16.04 with docker snap, it should be in
    # "/var/snap/docker/{version}/etc/docker/", but version could change.
    path = "/var/snap/docker/current/etc/docker/docker.conf"

    written = False
    if os.path.exists(path):
        try:
            with open(path, "w") as output:
                output.write(DOCKER_CONF)
            written = True
        except Exception as e:
            out.warn('Error writing to {}: {}'.format(path, str(e)))

    if not written:
        out.warn('Could not write docker configuration.')
    return written


def prepare_image(update, service):
    """
    Prepare a Docker image for execution.

    This is usually the longest operation during a chute installation, so
    instead of running this step in the update thread, we spin off a worker
    thread and return a Deferred. This will suspend processing of the current
    update until the worker thread finishes.
    """
    client = docker.APIClient(base_url="unix://var/run/docker.sock", version='auto')

    image_name = service.get_image_name()

    def call(func, *args, **kwargs):
        return func(*args, **kwargs)

    if settings.CONCURRENT_BUILDS:
        wrapper = deferToThread
    else:
        wrapper = call

    if service.type == "image":
        return wrapper(_pull_image, update, client, image_name)

    elif service.type == "inline":
        return wrapper(_build_image, update, service, client, True,
                rm=True, tag=image_name, fileobj=service.dockerfile)

    else:
        return wrapper(_build_image, update, service, client, False,
                rm=True, tag=image_name, path=update.workdir)


def check_image(update, service):
    """
    Check if image exists.
    """
    image_name = service.get_image_name()

    client = docker.DockerClient(base_url="unix://var/run/docker.sock",
            version='auto')

    # Raises an exception if the image does not exist.
    client.images.get(image_name)


def remove_image(update, service):
    """
    Remove a Docker image.
    """
    client = docker.APIClient(base_url="unix://var/run/docker.sock", version='auto')

    image_name = service.get_image_name()
    out.info("Removing image {}\n".format(image_name))

    try:
        client = docker.DockerClient(base_url="unix://var/run/docker.sock",
                version='auto')
        client.images.remove(image=image_name)
    except Exception as error:
        out.warn("Error removing image: {}".format(error))


def create_bridge(update):
    """
    Create a user-defined bridge network for the chute.
    """
    client = docker.DockerClient(base_url="unix://var/run/docker.sock", version='auto')
    client.networks.create(update.new.name, driver="bridge")


def remove_bridge(update):
    """
    Remove the bridge network associated with the chute.
    """
    client = docker.DockerClient(base_url="unix://var/run/docker.sock", version='auto')
    try:
        network = client.networks.get(update.new.name)
        network.remove()
    except docker.errors.NotFound:
        pass


def start_container(update, service):
    """
    Start running a service in a new container.
    """
    client = docker.DockerClient(base_url="unix://var/run/docker.sock", version='auto')

    container_name = service.get_container_name()
    image_name = service.get_image_name()
    out.info("Attempting to start container {} from image {}\n".format(
        container_name, image_name))

    host_config = build_host_config(update, service)

    # TODO
    # Set environment variables for the new container.
    # PARADROP_ROUTER_ID can be used to change application behavior based on
    # what router it is running on.
    environment = prepare_environment(update, service)

    try:
        container = client.containers.run(detach=True, image=image_name,
                name=container_name, environment=environment, **host_config)
        out.info("Successfully started chute with Id: %s\n" % (str(container.id)))
    except Exception as e:
        raise e

    try:
        network = client.networks.get(update.new.name)
        network.connect(container_name, aliases=[service.name])
    except docker.errors.NotFound:
        out.warn("Bridge network {} not found; connectivity between containers is limited.".format(update.new.name))


def remove_container(update, service):
    """
    Remove a service's container.
    """
    container_name = service.get_container_name()
    out.info("Removing container {}\n".format(container_name))

    try:
        client = docker.DockerClient(base_url="unix://var/run/docker.sock",
                version='auto')

        # Grab the last 40 log messages to help with debugging.
        container = client.containers.get(container_name)
        logs = container.logs(stream=False, tail=40, timestamps=False)
        update.progress("{}: {}".format(container_name, logs.rstrip()))

        container.remove(force=True)
    except Exception as error:
        out.warn("Error removing container: {}".format(error))


def _build_image(update, service, client, inline, **buildArgs):
    """
    Build the Docker image and monitor progress (worker function).

    inline: whether Dockerfile is specified as a string or a file in the
    working path.
    """
    # If this is a light chute, generate a Dockerfile.
    if service.type == "light":
        buildArgs['pull'] = True

        dockerfile = Dockerfile(service)
        valid, reason = dockerfile.isValid()
        if not valid:
            raise Exception("Invalid configuration: {}".format(reason))

        if inline:
            # Pass the dockerfile string directly.
            buildArgs['fileobj'] = dockerfile.getBytesIO()
        else:
            # Write it out to a file in the working directory.
            path = os.path.join(buildArgs['path'], "Dockerfile")
            dockerfile.writeFile(path)

    output = client.build(**buildArgs)

    buildSuccess = True
    for line in output:
        #if we encountered an error make note of it
        if 'errorDetail' in line:
            buildSuccess = False

        for key, value in json.loads(line).iteritems():
            if isinstance(value, dict):
                continue
            else:
                msg = value.rstrip()
                if len(msg) > 0 and suppress_re.match(msg) is None:
                    update.progress(msg)

    if not buildSuccess:
        raise Exception("Error building Docker image")


def _pull_image(update, client, image_name):
    """
    Pull the image from a registry.

    Returns True on success, False on failure.
    """
    #auth_config = {
    #    'username': settings.REGISTRY_USERNAME,
    #    'password': settings.REGISTRY_PASSWORD
    #}

    update.progress("Pulling image: {}".format(image_name))

    layers = 0
    complete = 0

    output = client.pull(image_name, auth_config=None, stream=True)
    for line in output:
        data = json.loads(line)

        # Suppress lines that have progressDetail set.  Those are the ones with
        # the moving progress bar.
        if data.get('progressDetail', {}) == {}:
            if 'status' not in data or 'id' not in data:
                continue

            update.progress("{}: {}".format(data['status'], data['id']))

            # Count the number of layers that need to be pulled and the number
            # completed.
            status = data['status'].strip().lower()
            if status == 'pulling fs layer':
                layers += 1
            elif status == 'pull complete':
                complete += 1

    update.progress("Finished pulling {} / {} layers".format(complete, layers))
    if complete < layers:
        raise Exception("Error pulling Docker image")


def stopChute(update):
    """
    Stop a docker container based on the passed in update.

    :param update: The update object containing information about the chute.
    :type update: obj
    :returns: None
    """
    out.info('Attempting to stop chute %s\n' % (update.name))

    c = docker.DockerClient(base_url='unix://var/run/docker.sock', version='auto')
    container = c.containers.get(update.name)
    container.stop()


def restartChute(update):
    """
    Start a docker container based on the passed in update.

    :param update: The update object containing information about the chute.
    :type update: obj
    :returns: None
    """
    out.info('Attempting to restart chute %s\n' % (update.name))
    c = docker.DockerClient(base_url='unix://var/run/docker.sock', version='auto')
    container = c.containers.get(update.name)
    container.start()


def getBridgeGateway():
    """
    Look up the gateway IP address for the docker bridge network.

    This is the docker0 IP address; it is the IP address of the host from the
    chute's perspective.
    """
    client = docker.DockerClient(base_url="unix://var/run/docker.sock", version='auto')

    network = client.networks.get("bridge")
    for config in network.attrs['IPAM']['Config']:
        if 'Gateway' in config:
            return config['Gateway']

    # Fall back to a default if we could not find it.  This address will work
    # in most places unless Docker changes to use a different address.
    out.warn('Could not find bridge gateway, using default')
    return '172.17.0.1'


def prepare_port_bindings(service):
    bindings = service.requests.get("port-bindings", {}).copy()

    # If the chute is configured to host a web service, check
    # whether there is a host port binding associated with it.
    # If not, we will add blank one so that Docker dynamically
    # assigns a port in the host to forward to the container.
    web_port = service.chute.web.get("port", None)
    web_service = service.chute.web.get("service", None)
    if web_port is not None and web_service == service.name:
        # The port could appear in multiple formats, e.g. 80, 80/tcp.
        # If any of them are present, we do not need to add anything.
        keys = [web_port, str(web_port), "{}/tcp".format(web_port)]
        if not any(k in bindings for k in keys):
            bindings["{}/tcp".format(web_port)] = None

    # Check if there is an existing version of the chute installed, so we can
    # potentially inherit old port bindings.
    try:
        container = ChuteContainer(service.get_container_name())
        data = container.inspect()
        old_bindings = data['NetworkSettings']['Ports']
    except ChuteNotFound:
        old_bindings = {}

    # If the current binding is unspecified (None), and there exists previous
    # binding for the same port, we can inherit the previous binding rather
    # than let Docker pick an arbitrary new one.
    #
    # The primary effect is that if a user followed a redirect to the web port
    # of a chute, then updated the chute, the port should remain valid after
    # the update.
    for key, value in bindings.iteritems():
        if value is None and key in old_bindings:
            port = old_bindings[key][0]['HostPort']
            bindings[key] = port

    return bindings


def build_host_config(update, service):
    """
    Build the host_config dict for a docker container based on the passed in update.

    :param chute: The chute object containing information about the chute.
    :type chute: obj
    :returns: (dict) The host_config dict which docker needs in order to create the container.
    """
    extra_hosts = {}
    network_mode = service.requests.get('network-mode', 'bridge')
    volumes = update.cache_get('volumes')

    # We are not able to set extra_hosts if the network_mode is set to 'host'.
    # In that case, the chute uses the same /etc/hosts file as the host system.
    if network_mode != 'host':
        extra_hosts[settings.LOCAL_DOMAIN] = getBridgeGateway()

    # If the chute has not configured a host binding for port 80, let Docker
    # assign a dynamic one.  We will use it to redirect HTTP requests to the
    # chute.
    port_bindings = update.cache_get('portBindings:{}'.format(service.name))

    # restart_policy: set to 'no' to prevent Docker from starting containers
    # automatically on system boot.  Paradrop will set up the host environment
    # first, then restart the containers.
    # host_conf = client.create_host_config(
    host_conf = dict(
        cap_add=['NET_ADMIN'],
        cap_drop=[],
        #devices=config.get('devices', []),
        #dns=config.get('dns'),
        dns_search=[],
        extra_hosts=extra_hosts,
        network_mode=network_mode,
        ports=port_bindings,
        privileged=service.requests.get('privileged', False),
        publish_all_ports=False,
        restart_policy={'Name': 'no'},
        volumes=volumes
    )
    return host_conf


def call_retry(cmd, env, delay=3, tries=3):
    # Make sure each component of the command is a string.  Otherwisew we will
    # get errors.
    clean_cmd = [str(v) for v in cmd]

    while tries >= 0:
        tries -= 1

        out.info("Calling: {}\n".format(" ".join(clean_cmd)))
        try:
            proc = subprocess.Popen(clean_cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
            for line in proc.stdout:
                out.info("{}: {}\n".format(clean_cmd[0], line.strip()))
            for line in proc.stderr:
                out.warn("{}: {}\n".format(clean_cmd[0], line.strip()))
            return proc.returncode
        except OSError as e:
            out.warn('Command "{}" failed\n'.format(" ".join(clean_cmd)))
            if tries <= 0:
                out.exception(e, True)
                raise e

        time.sleep(delay)


def setup_net_interfaces(update):
    """
    Link interfaces in the host to the internal interfaces in the Docker
    container.

    The commands are based on the pipework script
    (https://github.com/jpetazzo/pipework).

    :param chute: The chute object containing information about the chute.
    :type update: obj
    :returns: None
    """
    interfaces = update.cache_get('networkInterfaces')

    # Construct environment for subprocess calls.
    env = {
        "PATH": os.environ.get("PATH", "/bin")
    }
    if settings.DOCKER_BIN_DIR not in env['PATH']:
        env['PATH'] += ":" + settings.DOCKER_BIN_DIR

    # Keep list of interfaces that assign to the Docker container that we will
    # need to recover when stopping the chute, for example monitor mode
    # interfaces.  They will not automatically return to the default namespace,
    # so we need to keep track of them.
    borrowedInterfaces = []

    for iface in interfaces:
        itype = iface.get('type', 'wifi')
        mode = iface.get('mode', 'ap')

        service = update.new.get_service(iface['service'])

        # We need the container's PID in order to work with Linux namespaces.
        container = ChuteContainer(service.get_container_name())
        pid = container.getPID()

        if itype == 'lan' or itype == 'vlan' or (itype == 'wifi' and mode == 'ap'):
            IP = iface['ipaddrWithPrefix']
            internalIntf = iface['internalIntf']
            externalIntf = iface['externalIntf']

            # Generate a temporary interface name.  It just needs to be unique.
            # We will rename to the internalIntf name as soon as the interface
            # is inside the chute.
            tmpIntf = "tmp{:x}".format(random.getrandbits(32))

            # TODO copy MTU from original interface?
            cmd = ['ip', 'link', 'add', 'link', externalIntf, 'dev', tmpIntf,
                    'type', 'macvlan', 'mode', 'bridge']
            call_retry(cmd, env, tries=1)

            # Bring the interface up.
            cmd = ['ip', 'link', 'set', tmpIntf, 'up']
            call_retry(cmd, env, tries=1)

            # Give the new interface to the chute.
            cmd = ['ip', 'link', 'set', tmpIntf, 'netns', str(pid)]
            call_retry(cmd, env, tries=1)

            # Rename the interface according to what the chute wants.
            cmd = ['ip', 'link', 'set', tmpIntf, 'name', internalIntf]
            call_in_netns(service, env, cmd)

            # Set the IP address.
            cmd = ['ip', 'addr', 'add', IP, 'dev', internalIntf]
            call_in_netns(service, env, cmd)

            # Bring the interface up again.
            cmd = ['ip', 'link', 'set', internalIntf, 'up']
            call_in_netns(service, env, cmd)

        elif itype == 'wifi' and mode == 'monitor':
            internalIntf = iface['internalIntf']
            externalIntf = iface['externalIntf']
            phyname = iface['phy']

            cmd = ['iw', 'phy', phyname, 'set', 'netns', str(pid)]
            call_retry(cmd, env, tries=1)

            # Rename the interface inside the container.
            cmd = ['ip', 'link', 'set', 'dev', externalIntf, 'up', 'name',
                    internalIntf]
            call_in_netns(service, env, cmd)

            borrowedInterfaces.append({
                'type': 'wifi',
                'pid': pid,
                'internal': internalIntf,
                'external': externalIntf,
                'phy': phyname
            })

        elif itype == '_lan':
            # Not currently supported, this mode would allow chutes to take
            # control of the physical LAN interface directly, rather than the
            # virtual macvlan interface created above.
            internalIntf = iface['internalIntf']
            externalIntf = iface['externalIntf']

            cmd = ['ip', 'link', 'set', 'dev', externalIntf, 'up', 'netns',
                    str(pid), 'name', internalIntf]
            call_retry(cmd, env, tries=1)

            borrowedInterfaces.append({
                'type': 'lan',
                'pid': pid,
                'internal': internalIntf,
                'external': externalIntf
            })

    update.cache_set('borrowedInterfaces', borrowedInterfaces)


def cleanup_net_interfaces(update):
    """
    Cleanup special interfaces when bringing down a container.

    This applies to monitor mode interfaces, which need to be renamed before
    they come back to the host network, e.g. "mon0" inside the container should
    be renamed to the appropriate "wlanX" before the container exits.
    """
    borrowedInterfaces = update.cache_get('borrowedInterfaces')
    if borrowedInterfaces is None:
        return

    # Construct environment for subprocess calls.
    env = {
        "PATH": os.environ.get("PATH", "/bin")
    }
    if settings.DOCKER_BIN_DIR not in env['PATH']:
        env['PATH'] += ":" + settings.DOCKER_BIN_DIR

    for iface in borrowedInterfaces:
        service = update.new.get_service(iface['service'])

        if iface['type'] == 'wifi':
            cmd = ['ip', 'link', 'set', 'dev', iface['internal'], 'down',
                    'name', iface['external']]
            call_in_netns(service, env, cmd, onerror="ignore", pid=iface['pid'])

            cmd = ['iw', 'phy', iface['phy'], 'set', 'netns', '1']
            call_in_netns(service, env, cmd, onerror="ignore", pid=iface['pid'])

            resetWirelessDevice(iface['phy'], iface['external'])

        elif iface['type'] == 'lan':
            cmd = ['ip', 'link', 'set', 'dev', iface['internal'], 'down',
                    'netns', '1', 'name', iface['external']]
            call_in_netns(service, env, cmd, onerror="ignore", pid=iface['pid'])


def call_in_netns(service, env, command, onerror="raise", pid=None):
    """
    Call command within a service's namespace.

    command: should be a list of strings.
    onerror: should be "raise" or "ignore"
    """
    container_name = service.get_container_name()

    if pid is None:
        # We need the chute's PID in order to work with Linux namespaces.
        container = ChuteContainer(container_name)
        pid = container.getPID()

    # Try first with `nsenter`.  This is preferred because it works using
    # commands in the host.  We cannot be sure the `docker exec` version will
    # work with all chute images.
    cmd = ['nsenter', '--target', str(pid), '--net', '--no-fork'] + command
    try:
        code = call_retry(cmd, env, tries=1)
    except:
        code = -1

    # We fall back to `docker exec` which relies on the container image having
    # an `ip` command available to configure interfaces from within.
    if code != 0:
        out.warn("nsenter command failed, resorting to docker exec\n")

        try:
            client = docker.DockerClient(base_url="unix://var/run/docker.sock", version='auto')
            container = client.containers.get(container_name)
            container.exec_run(command, user='root')
        except Exception:
            if onerror == "raise":
                raise


def prepare_environment(update, service):
    """
    Prepare environment variables for a chute container.
    """
    # Make a copy so that we do not alter the original, which only contains
    # user-specified environment variables.
    #
    # Start with the chute environment variables, then apply service-specific
    # variables on top of that.
    env = update.new.get_environment().copy()
    env.update(service.environment)

    env['PARADROP_CHUTE_NAME'] = update.new.name
    env['PARADROP_FEATURES'] = constants.DAEMON_FEATURES
    env['PARADROP_ROUTER_ID'] = nexus.core.info.pdid
    env['PARADROP_DATA_DIR'] = update.cache_get('internalDataDir')
    env['PARADROP_SYSTEM_DIR'] = update.cache_get('internalSystemDir')
    env['PARADROP_API_URL'] = "http://{}/api".format(settings.LOCAL_DOMAIN)
    env['PARADROP_BASE_URL'] = "http://{}/api/v1/chutes/{}".format(
            settings.LOCAL_DOMAIN, update.new.name)
    env['PARADROP_API_TOKEN'] = update.cache_get('apiToken')
    env['PARADROP_WS_API_URL'] = "ws://{}/ws".format(settings.LOCAL_DOMAIN)
    env['PARADROP_CHUTE_VERSION'] = update.new.version

    return env


def _setResourceAllocation(allocation):
    client = docker.DockerClient(base_url="unix://var/run/docker.sock", version='auto')
    for container_name, resources in six.iteritems(allocation):
        out.info("Update chute {} set cpu_shares={}\n".format(
            container_name, resources['cpu_shares']))
        container = client.containers.get(container_name)
        container.update(cpu_shares=resources['cpu_shares'])

        # Using class id 1:1 for prioritized, 1:3 for best effort.
        # Prioritization is implemented in confd/qos.py.  Class-ID is
        # represented in hexadecimal.
        # Reference: https://www.kernel.org/doc/Documentation/cgroup-v1/net_cls.txt
        if resources.get('prioritize_traffic', False):
            classid = "0x10001"
        else:
            classid = "0x10003"

        container = ChuteContainer(container_name)
        try:
            container_id = container.getID()
            fname = "/sys/fs/cgroup/net_cls/docker/{}/net_cls.classid".format(container_id)
            with open(fname, "w") as output:
                output.write(classid)
        except Exception as error:
            out.warn("Error setting traffic class: {}\n".format(error))


def setResourceAllocation(update):
    """
    Adjust compute resources assigned to chute containers.
    """
    allocation = update.cache_get('newResourceAllocation')
    _setResourceAllocation(allocation)


def revertResourceAllocation(update):
    allocation = update.cache_get('oldResourceAllocation')
    _setResourceAllocation(allocation)


def removeAllContainers(update):
    """
    Remove all containers on the system.  This should only be used as part of a
    factory reset mechanism.

    :returns: None
    """
    client = docker.DockerClient(base_url='unix://var/run/docker.sock', version='auto')

    for container in client.containers.list(all=True):
        try:
            container.remove(force=True)
        except Exception as e:
            update.progress(str(e))
