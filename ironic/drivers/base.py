# -*- encoding: utf-8 -*-
#
# Copyright 2013 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""
Abstract base classes for drivers.
"""

import abc
import collections
import copy
import functools
import inspect
import json
import os

from oslo_log import log as logging
from oslo_utils import excutils

from ironic.common import exception
from ironic.common.i18n import _
from ironic.common import raid
from ironic.common import states

LOG = logging.getLogger(__name__)

RAID_CONFIG_SCHEMA = os.path.join(os.path.dirname(__file__),
                                  'raid_config_schema.json')

RAID_APPLY_CONFIGURATION_ARGSINFO = {
    "raid_config": {
        "description": "The RAID configuration to apply.",
        "required": True,
    },
    "create_root_volume": {
        "description": (
            "Setting this to 'False' indicates not to create root "
            "volume that is specified in 'raid_config'. Default "
            "value is 'True'."
        ),
        "required": False,
    },
    "create_nonroot_volumes": {
        "description": (
            "Setting this to 'False' indicates not to create "
            "non-root volumes (all except the root volume) in "
            "'raid_config'. Default value is 'True'."
        ),
        "required": False,
    },
    "delete_existing": {
        "description": (
            "Setting this to 'True' indicates to delete existing RAID "
            "configuration prior to creating the new configuration. "
            "Default value is 'True'."
        ),
        "required": False,
    }
}
"""
This may be used as the deploy_step argsinfo argument for RAID interfaces
implementing an apply_configuration deploy step.
"""


class BareDriver(object):
    """A bare driver object which will have interfaces attached later.

    Any composable interfaces should be added as class attributes of this
    class, as well as appended to core_interfaces or standard_interfaces here.
    """

    bios = None
    """`Standard` attribute for BIOS related features.

    A reference to an instance of :class:BIOSInterface.
    """

    boot = None
    """`Standard` attribute for boot related features.

    A reference to an instance of :class:BootInterface.
    """

    console = None
    """`Standard` attribute for managing console access.

    A reference to an instance of :class:ConsoleInterface.
    """

    deploy = None
    """`Core` attribute for managing deployments.

    A reference to an instance of :class:DeployInterface.
    """

    firmware = None
    """`Standard` attribute for inspection related features.

    A reference to an instance of :class:FirmwareInterface.
    """

    inspect = None
    """`Standard` attribute for inspection related features.

    A reference to an instance of :class:InspectInterface.
    """

    management = None
    """`Standard` attribute for management related features.

    A reference to an instance of :class:ManagementInterface.
    """

    network = None
    """`Core` attribute for network connectivity.

    A reference to an instance of :class:NetworkInterface.
    """

    power = None
    """`Core` attribute for managing power state.

    A reference to an instance of :class:PowerInterface.
    """

    raid = None
    """`Standard` attribute for RAID related features.

    A reference to an instance of :class:RaidInterface.
    """

    rescue = None
    """`Standard` attribute for accessing rescue features.

    A reference to an instance of :class:RescueInterface.
    """

    storage = None
    """`Standard` attribute for (remote) storage interface.

    A reference to an instance of :class:StorageInterface.
    """

    vendor = None
    """Attribute for accessing any vendor-specific extensions.

    A reference to an instance of :class:VendorInterface.
    """

    @property
    def core_interfaces(self):
        """Interfaces that are required to be implemented."""
        return ['boot', 'deploy', 'management', 'network', 'power']

    @property
    def optional_interfaces(self):
        """Interfaces that can be no-op."""
        return ['bios', 'console', 'firmware', 'inspect', 'raid', 'rescue',
                'storage']

    @property
    def all_interfaces(self):
        return self.non_vendor_interfaces + ['vendor']

    @property
    def non_vendor_interfaces(self):
        return list(self.core_interfaces + self.optional_interfaces)

    def get_properties(self):
        """Get the properties of the driver.

        :returns: dictionary of <property name>:<property description> entries.
        """

        properties = {}
        for iface_name in self.all_interfaces:
            iface = getattr(self, iface_name, None)
            if iface:
                properties.update(iface.get_properties())
        return properties


ALL_INTERFACES = set(BareDriver().all_interfaces)
"""Constant holding all known interfaces."""


class BaseInterface(object, metaclass=abc.ABCMeta):
    """A base interface implementing common functions for Driver Interfaces."""

    supported = True
    """Indicates if an interface is supported.

    This will be set to False for interfaces which are untested in first- or
    third-party CI, or in the process of being deprecated.
    """

    interface_type = 'base'
    """Interface type, used for clean steps and logging."""

    @abc.abstractmethod
    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """

    @abc.abstractmethod
    def validate(self, task):
        """Validate the driver-specific Node deployment info.

        This method validates whether the 'driver_info' and/or 'instance_info'
        properties of the task's node contains the required information for
        this interface to function.

        This method is often executed synchronously in API requests, so it
        should not conduct long-running checks.

        :param task: A TaskManager instance containing the node to act on.
        :raises: InvalidParameterValue on malformed parameter(s)
        :raises: MissingParameterValue on missing parameter(s)
        """

    def __new__(cls, *args, **kwargs):
        # Get the list of clean steps, deploy steps and verify steps when the
        # interface is initialized by the conductor.
        # We use __new__ instead of __init___
        # to avoid breaking backwards compatibility with all the drivers.
        # We want to return all steps, regardless of priority.

        super_new = super(BaseInterface, cls).__new__
        if super_new is object.__new__:
            instance = super_new(cls)
        else:
            instance = super_new(cls, *args, **kwargs)
        instance.clean_steps = []
        instance.deploy_steps = []
        instance.verify_steps = []
        instance.service_steps = []
        for n, method in inspect.getmembers(instance, inspect.ismethod):
            if getattr(method, '_is_clean_step', False):
                # Create a CleanStep to represent this method
                step = {'step': method.__name__,
                        'priority': method._clean_step_priority,
                        'abortable': method._clean_step_abortable,
                        'argsinfo': method._clean_step_argsinfo,
                        'interface': instance.interface_type,
                        'requires_ramdisk':
                            method._clean_step_requires_ramdisk}
                instance.clean_steps.append(step)
            if getattr(method, '_is_deploy_step', False):
                # Create a DeployStep to represent this method
                step = {'step': method.__name__,
                        'priority': method._deploy_step_priority,
                        'argsinfo': method._deploy_step_argsinfo,
                        'interface': instance.interface_type}
                instance.deploy_steps.append(step)
            if getattr(method, '_is_verify_step', False):
                # Create a VerifyStep to represent this method
                step = {'step': method.__name__,
                        'priority': method._verify_step_priority,
                        'interface': instance.interface_type}
                instance.verify_steps.append(step)
            if getattr(method, '_is_service_step', False):
                step = {'step': method.__name__,
                        'priority': method._service_step_priority,
                        'abortable': method._service_step_abortable,
                        'argsinfo': method._service_step_argsinfo,
                        'interface': instance.interface_type,
                        'requires_ramdisk':
                            method._service_step_requires_ramdisk}
                instance.service_steps.append(step)

        if instance.clean_steps:
            LOG.debug('Found clean steps %(steps)s for interface '
                      '%(interface)s',
                      {'steps': instance.clean_steps,
                       'interface': instance.interface_type})
        if instance.deploy_steps:
            LOG.debug('Found deploy steps %(steps)s for interface '
                      '%(interface)s',
                      {'steps': instance.deploy_steps,
                       'interface': instance.interface_type})
        if instance.verify_steps:
            LOG.debug('Found verify steps %(steps)s for interface '
                      '%(interface)s',
                      {'steps': instance.deploy_steps,
                       'interface': instance.interface_type})
        if instance.service_steps:
            LOG.debug('Found service steps %(steps)s for interface '
                      '%(interface)s',
                      {'steps': instance.service_steps,
                       'interface': instance.interface_type})

        return instance

    def _execute_step(self, task, step):
        """Execute the step on task.node.

        A step must take a single positional argument: a TaskManager
        object. It may take one or more keyword variable arguments.

        :param task: A TaskManager object
        :param step: The step dictionary representing the step to execute
        """
        args = step.get('args')
        if args is not None:
            return getattr(self, step['step'])(task, **args)
        else:
            return getattr(self, step['step'])(task)

    def get_clean_steps(self, task):
        """Get a list of (enabled and disabled) clean steps for the interface.

        This function will return all clean steps (both enabled and disabled)
        for the interface, in an unordered list.

        :param task: A TaskManager object, useful for interfaces overriding
            this function
        :raises NodeCleaningFailure: if there is a problem getting the steps
            from the driver. For example, when a node (using an agent driver)
            has just been enrolled and the agent isn't alive yet to be queried
            for the available clean steps.
        :returns: A list of clean step dictionaries
        """
        return self.clean_steps

    def execute_clean_step(self, task, step):
        """Execute the clean step on task.node.

        A clean step must take a single positional argument: a TaskManager
        object. It may take one or more keyword variable arguments (for
        use with manual cleaning only.)

        A step can be executed synchronously or asynchronously. A step should
        return None if the method has completed synchronously or
        states.CLEANWAIT if the step will continue to execute asynchronously.
        If the step executes asynchronously, it should issue a call to the
        'continue_node_clean' RPC, so the conductor can begin the next
        clean step.

        :param task: A TaskManager object
        :param step: The clean step dictionary representing the step to execute
        :returns: None if this method has completed synchronously, or
            states.CLEANWAIT if the step will continue to execute
            asynchronously.
        """
        return self._execute_step(task, step)

    def get_deploy_steps(self, task):
        """Get a list of (enabled and disabled) deploy steps for the interface.

        This function will return all deploy steps (both enabled and disabled)
        for the interface, in an unordered list.

        :param task: A TaskManager object, useful for interfaces overriding
            this function
        :raises InstanceDeployFailure: if there is a problem getting the steps
            from the driver. For example, when a node (using an agent driver)
            has just been enrolled and the agent isn't alive yet to be queried
            for the available deploy steps.
        :returns: A list of deploy step dictionaries
        """
        return self.deploy_steps

    def execute_deploy_step(self, task, step):
        """Execute the deploy step on task.node.

        A deploy step must take a single positional argument: a TaskManager
        object. It may take one or more keyword variable arguments (for
        use in the future, when deploy steps can be specified via the API).

        A step can be executed synchronously or asynchronously. A step should
        return None if the method has completed synchronously or
        states.DEPLOYWAIT if the step will continue to execute asynchronously.
        If the step executes asynchronously, it should issue a call to the
        'continue_node_deploy' RPC, so the conductor can begin the next
        deploy step.

        :param task: A TaskManager object
        :param step: The deploy step dictionary representing the step to
            execute
        :returns: None if this method has completed synchronously, or
            states.DEPLOYWAIT if the step will continue to execute
            asynchronously.
        """
        return self._execute_step(task, step)

    def get_verify_steps(self, task):
        """Get a list of (enabled and disabled) verify steps for the interface.

        This function will return all verify steps (both enabled and disabled)
        for the interface, in an unordered list.

        :param task: A TaskManager object, useful for interfaces overriding
            this function
        :raises NodeVerifyFailure: if there is a problem getting the steps
            from the driver. For example, when a node (using an agent driver)
            has just been enrolled and the agent isn't alive yet to be queried
            for the available verify steps.
        :returns: A list of verify step dictionaries
        """
        return self.verify_steps

    def execute_verify_step(self, task, step):
        """Execute the verify step on task.node.

        A verify step must take a single positional argument: a TaskManager
        object. It does not take keyword variable arguments.

        :param task: A TaskManager object
        :param step: The verify step dictionary representing the step to
            execute
        :returns: None if this method has completed synchronously
        """
        return self._execute_step(task, step)

    def get_service_steps(self, task):
        """Get a list of service steps for the interface.

        This function will return all service steps (both enabled and disabled)
        for the interface, in an unordered list.

        :param task: A TaskManager object, useful for interfaces overriding
            this function
        :raises NodeServiceFailure: if there is a problem getting the steps
            from the driver. For example, when a node (using an agent driver)
            has just been enrolled and the agent isn't alive yet to be queried
            for the available clean steps.
        :returns: A list of service step dictionaries
        """
        return self.service_steps

    def execute_service_step(self, task, step):
        """Execute the service step on task.node.

        A service step must take a single positional argument: a TaskManager
        object. It does not take keyword variable arguments.

        :param task: A TaskManager object
        :param step: The service step dictionary representing the step to
            execute
        :returns: None if this method has completed synchronously
        """
        return self._execute_step(task, step)


class DeployInterface(BaseInterface):
    """Interface for deploy-related actions."""
    interface_type = 'deploy'

    @abc.abstractmethod
    def deploy(self, task):
        """Perform a deployment to the task's node.

        Perform the necessary work to deploy an image onto the specified node.
        This method will be called after prepare(), which may have already
        performed any preparatory steps, such as pre-caching some data for the
        node.

        :param task: A TaskManager instance containing the node to act on.
        :returns: status of the deploy. One of ironic.common.states.
        """

    @abc.abstractmethod
    def tear_down(self, task):
        """Tear down a previous deployment on the task's node.

        Given a node that has been previously deployed to,
        do all cleanup and tear down necessary to "un-deploy" that node.

        :param task: A TaskManager instance containing the node to act on.
        :returns: status of the deploy. One of ironic.common.states.
        """

    @abc.abstractmethod
    def prepare(self, task):
        """Prepare the deployment environment for the task's node.

        If preparation of the deployment environment ahead of time is possible,
        this method should be implemented by the driver.

        If implemented, this method must be idempotent. It may be called
        multiple times for the same node on the same conductor.

        This method is called before `deploy`.

        :param task: A TaskManager instance containing the node to act on.
        """

    @abc.abstractmethod
    def clean_up(self, task):
        """Clean up the deployment environment for the task's node.

        If preparation of the deployment environment ahead of time is possible,
        this method should be implemented by the driver. It should erase
        anything cached by the `prepare` method.

        If implemented, this method must be idempotent. It may be called
        multiple times for the same node on the same conductor, and it may be
        called by multiple conductors in parallel. Therefore, it must not
        require an exclusive lock.

        This method is called before `tear_down`.

        :param task: A TaskManager instance containing the node to act on.
        """

    @abc.abstractmethod
    def take_over(self, task):
        """Take over management of this task's node from a dead conductor.

        If conductors' hosts maintain a static relationship to nodes, this
        method should be implemented by the driver to allow conductors to
        perform the necessary work during the remapping of nodes to conductors
        when a conductor joins or leaves the cluster.

        For example, the PXE driver has an external dependency:
            Neutron must forward DHCP BOOT requests to a conductor which has
            prepared the tftpboot environment for the given node. When a
            conductor goes offline, another conductor must change this setting
            in Neutron as part of remapping that node's control to itself.
            This is performed within the `takeover` method.

        :param task: A TaskManager instance containing the node to act on.
        """

    def prepare_cleaning(self, task):
        """Prepare the node for cleaning tasks.

        For example, nodes that use the Ironic Python Agent will need to
        boot the ramdisk in order to do in-band cleaning tasks.

        If the function is asynchronous, the driver will need to handle
        settings node.driver_internal_info['clean_steps'] and node.clean_step,
        as they would be set in ironic.conductor.manager._do_node_clean,
        but cannot be set when this is asynchronous. After, the interface
        should make an RPC call to continue_node_cleaning to start cleaning.

        NOTE(JoshNang) this should be moved to BootInterface when it gets
        implemented.

        :param task: A TaskManager instance containing the node to act on.
        :returns: If this function is going to be asynchronous, should return
            `states.CLEANWAIT`. Otherwise, should return `None`. The interface
            will need to call _get_cleaning_steps and then RPC to
            continue_node_cleaning
        """
        pass

    def tear_down_cleaning(self, task):
        """Tear down after cleaning is completed.

        Given that cleaning is complete, do all cleanup and tear
        down necessary to allow the node to be deployed to again.

        NOTE(JoshNang) this should be moved to BootInterface when it gets
        implemented.

        :param task: A TaskManager instance containing the node to act on.
        """
        pass

    def heartbeat(self, task, callback_url, agent_version,
                  agent_verify_ca=None, agent_status=None,
                  agent_status_message=None):
        """Record a heartbeat for the node.

        :param task: A TaskManager instance containing the node to act on.
        :param callback_url: a URL to use to call to the ramdisk.
        :param agent_version: The version of the agent that is heartbeating
        :param agent_verify_ca: TLS certificate for the agent.
        :param agent_status: Status of the heartbeating agent
        :param agent_status_message: Message describing the agent status
        :return: None
        """
        LOG.warning('Got heartbeat message from node %(node)s, but '
                    'the driver %(driver)s does not support heartbeating',
                    {'node': task.node.uuid, 'driver': task.node.driver})

    def tear_down_service(self, task):
        """Tear down after servicing is completed.

        Given that servicing is complete, do all cleanup and tear
        down necessary to allow the node to be returned to an active
        state.

        :param task: A TaskManager instance containing the node to act on.
        """
        pass

    def prepare_service(self, task):
        """Prepare the node for servicing tasks.

        For example, nodes that use the Ironic Python Agent will need to
        boot the ramdisk in order to do in-band service tasks.

        If the function is asynchronous, the driver will need to handle
        settings node.driver_internal_info['service_steps'] and
        node.service_step, as they would be set in
        ironic.conductor.manager._do_node_service, but cannot be set when
        this is asynchronous. After, the interface should make an RPC call
        to continue_node_servicing to start cleaning.

        :param task: A TaskManager instance containing the node to act on.
        :returns: If this function is going to be asynchronous, should return
            `states.SERVICEWAIT`. Otherwise, should return `None`.
            The interface will need to call _get_cleaning_steps and then RPC
            to continue_node_service.
        """
        pass


class BootInterface(BaseInterface):
    """Interface for boot-related actions."""
    interface_type = 'boot'
    capabilities = []

    @abc.abstractmethod
    def prepare_ramdisk(self, task, ramdisk_params):
        """Prepares the boot of Ironic ramdisk.

        This method prepares the boot of the deploy or rescue ramdisk after
        reading relevant information from the node's database.

        :param task: A task from TaskManager.
        :param ramdisk_params: The options to be passed to the ironic ramdisk.
            Different implementations might want to boot the ramdisk in
            different ways by passing parameters to them.  For example,

            When Agent ramdisk is booted to deploy a node, it takes the
            parameters ipa-api-url, etc.

            Other implementations can make use of ramdisk_params to pass such
            information.  Different implementations of boot interface will
            have different ways of passing parameters to the ramdisk.
        :returns: None
        """

    @abc.abstractmethod
    def clean_up_ramdisk(self, task):
        """Cleans up the boot of ironic ramdisk.

        This method cleans up the environment that was setup for booting the
        deploy or rescue ramdisk.

        :param task: A task from TaskManager.
        :returns: None
        """

    @abc.abstractmethod
    def prepare_instance(self, task):
        """Prepares the boot of instance.

        This method prepares the boot of the instance after reading
        relevant information from the node's database.

        :param task: A task from TaskManager.
        :returns: None
        """

    @abc.abstractmethod
    def clean_up_instance(self, task):
        """Cleans up the boot of instance.

        This method cleans up the environment that was setup for booting
        the instance.

        :param task: A task from TaskManager.
        :returns: None
        """

    def validate_rescue(self, task):
        """Validate that the node has required properties for rescue.

        :param task: A TaskManager instance with the node being checked
        :raises: MissingParameterValue if node is missing one or more required
            parameters
        :raises: UnsupportedDriverExtension
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='validate_rescue')

    def validate_inspection(self, task):
        """Validate that the node has required properties for inspection.

        :param task: A TaskManager instance with the node being checked
        :raises: MissingParameterValue if node is missing one or more required
            parameters
        :raises: UnsupportedDriverExtension
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='validate_inspection')


class PowerInterface(BaseInterface):
    """Interface for power-related actions."""
    interface_type = 'power'

    @abc.abstractmethod
    def get_power_state(self, task):
        """Return the power state of the task's node.

        :param task: A TaskManager instance containing the node to act on.
        :raises: MissingParameterValue if a required parameter is missing.
        :returns: A power state. One of :mod:`ironic.common.states`.
        """

    @abc.abstractmethod
    def set_power_state(self, task, power_state, timeout=None):
        """Set the power state of the task's node.

        :param task: A TaskManager instance containing the node to act on.
        :param power_state: Any power state from :mod:`ironic.common.states`.
        :param timeout: timeout (in seconds) positive integer (> 0) for any
          power state. ``None`` indicates to use default timeout.
        :raises: MissingParameterValue if a required parameter is missing.
        """

    @abc.abstractmethod
    def reboot(self, task, timeout=None):
        """Perform a hard reboot of the task's node.

        Drivers are expected to properly handle case when node is powered off
        by powering it on.

        :param task: A TaskManager instance containing the node to act on.
        :param timeout: timeout (in seconds) positive integer (> 0) for any
          power state. ``None`` indicates to use default timeout.
        :raises: MissingParameterValue if a required parameter is missing.
        """

    def get_supported_power_states(self, task):
        """Get a list of the supported power states.

        :param task: A TaskManager instance containing the node to act on.
        :returns: A list with the supported power states defined
                  in :mod:`ironic.common.states`.
        """
        return [states.POWER_ON, states.POWER_OFF, states.REBOOT]

    def supports_power_sync(self, task):
        """Check if power sync is supported for the given node.

        If ``False``, the conductor will simply store whatever
        ``get_power_state`` returns in the database instead of trying
        to force the expected power state.

        :param task: A TaskManager instance containing the node to act on.
        :returns: boolean, whether power sync is supported.
        """
        return True


class ConsoleInterface(BaseInterface):
    """Interface for console-related actions."""
    interface_type = "console"

    @abc.abstractmethod
    def start_console(self, task):
        """Start a remote console for the task's node.

        This method should not raise an exception if console already started.

        :param task: A TaskManager instance containing the node to act on.
        """

    @abc.abstractmethod
    def stop_console(self, task):
        """Stop the remote console session for the task's node.

        :param task: A TaskManager instance containing the node to act on.
        """

    @abc.abstractmethod
    def get_console(self, task):
        """Get connection information about the console.

        This method should return the necessary information for the
        client to access the console.

        :param task: A TaskManager instance containing the node to act on.
        :returns: the console connection information.
        """


class RescueInterface(BaseInterface):
    """Interface for rescue-related actions."""
    interface_type = "rescue"

    @abc.abstractmethod
    def rescue(self, task):
        """Boot the task's node into a rescue environment.

        :param task: A TaskManager instance containing the node to act on.
        :raises: InstanceRescueFailure if node validation or rescue operation
                 fails.
        :returns: states.RESCUEWAIT if rescue is in progress asynchronously
                  or states.RESCUE if it is complete.
        """

    @abc.abstractmethod
    def unrescue(self, task):
        """Tear down the rescue environment, and return to normal.

        :param task: A TaskManager instance containing the node to act on.
        :raises: InstanceUnrescueFailure if node validation or unrescue
                 operation fails.
        :returns: states.ACTIVE if it is successful.
        """

    def clean_up(self, task):
        """Clean up the rescue environment for the task's node.

        This is particularly useful for nodes where rescuing is asynchronous
        and a timeout occurs.

        :param task: A TaskManager instance containing the node to act on.
        :returns: None
        """
        pass


# Representation of a single vendor method metadata
VendorMetadata = collections.namedtuple('VendorMetadata', ['method',
                                                           'metadata'])


def _passthru(http_methods, method=None, async_call=True,
              driver_passthru=False, description=None,
              attach=False, require_exclusive_lock=True):
    """A decorator for registering a function as a passthru function.

    Decorator ensures function is ready to catch any ironic exceptions
    and reraise them after logging the issue. It also catches non-ironic
    exceptions reraising them as a VendorPassthruException after writing
    a log.

    Logs need to be added because even though the exception is being
    reraised, it won't be handled if it is an async. call.

    :param http_methods: A list of supported HTTP methods by the vendor
                         function.
    :param method: an arbitrary string describing the action to be taken.
    :param async_call: Boolean value. If True invoke the passthru function
                  asynchronously; if False, synchronously. If a passthru
                  function touches the BMC we strongly recommend it to
                  run asynchronously. Defaults to True.
    :param driver_passthru: Boolean value. True if this is a driver vendor
                            passthru method, and False if it is a node
                            vendor passthru method.
    :param attach: Boolean value. True if the return value should be
                   attached to the response object, and False if the return
                   value should be returned in the response body.
                   Defaults to False.
    :param description: a string shortly describing what the method does.
    :param require_exclusive_lock: Boolean value. Only valid for node passthru
                                   methods. If True, lock the node before
                                   validate() and invoking the vendor method.
                                   The node remains locked during execution
                                   for a synchronous passthru method. If False,
                                   don't lock the node. Defaults to True.
    """

    def handle_passthru(func):
        api_method = method
        if api_method is None:
            api_method = func.__name__

        supported_ = [i.upper() for i in http_methods]
        description_ = description or ''
        metadata = VendorMetadata(api_method, {'http_methods': supported_,
                                               'async': async_call,
                                               'description': description_,
                                               'attach': attach})
        if driver_passthru:
            func._driver_metadata = metadata
        else:
            metadata[1]['require_exclusive_lock'] = require_exclusive_lock
            func._vendor_metadata = metadata

        passthru_logmessage = 'vendor_passthru failed with method %s'

        @functools.wraps(func)
        def passthru_handler(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except exception.IronicException:
                with excutils.save_and_reraise_exception():
                    LOG.exception(passthru_logmessage, api_method)
            except Exception as e:
                # catch-all in case something bubbles up here
                LOG.exception(passthru_logmessage, api_method)
                raise exception.VendorPassthruException(message=e)
        return passthru_handler
    return handle_passthru


def passthru(http_methods, method=None, async_call=True, description=None,
             attach=False, require_exclusive_lock=True):
    return _passthru(http_methods, method, async_call,
                     driver_passthru=False,
                     description=description, attach=attach,
                     require_exclusive_lock=require_exclusive_lock)


def driver_passthru(http_methods, method=None, async_call=True,
                    description=None, attach=False):
    return _passthru(http_methods, method, async_call,
                     driver_passthru=True, description=description,
                     attach=attach)


class VendorInterface(BaseInterface):
    """Interface for all vendor passthru functionality.

    Additional vendor- or driver-specific capabilities should be
    implemented as a method in the class inheriting from this class and
    use the @passthru or @driver_passthru decorators.

    Methods decorated with @driver_passthru should be short-lived because
    it is a blocking call.
    """
    interface_type = "vendor"

    def __new__(cls, *args, **kwargs):
        super_new = super(VendorInterface, cls).__new__
        if super_new is object.__new__:
            inst = super_new(cls)
        else:
            inst = super_new(cls, *args, **kwargs)

        inst.vendor_routes = {}
        inst.driver_routes = {}

        for name, ref in inspect.getmembers(inst, predicate=inspect.ismethod):
            vmeta = getattr(ref, '_vendor_metadata', None)
            dmeta = getattr(ref, '_driver_metadata', None)

            if vmeta is not None:
                metadata = copy.deepcopy(vmeta.metadata)
                metadata['func'] = ref
                inst.vendor_routes.update({vmeta.method: metadata})

            if dmeta is not None:
                metadata = copy.deepcopy(dmeta.metadata)
                metadata['func'] = ref
                inst.driver_routes.update({dmeta.method: metadata})

        return inst

    @abc.abstractmethod
    def validate(self, task, method=None, **kwargs):
        """Validate vendor-specific actions.

        If invalid, raises an exception; otherwise returns None.

        :param task: A task from TaskManager.
        :param method: Method to be validated
        :param kwargs: Info for action.
        :raises: UnsupportedDriverExtension if 'method' can not be mapped to
                 the supported interfaces.
        :raises: InvalidParameterValue if kwargs does not contain 'method'.
        :raises: MissingParameterValue
        """

    def driver_validate(self, method, **kwargs):
        """Validate driver-vendor-passthru actions.

        If invalid, raises an exception; otherwise returns None.

        :param method: method to be validated
        :param kwargs: info for action.
        :raises: MissingParameterValue if kwargs does not contain
                 certain parameter.
        :raises: InvalidParameterValue if parameter does not match.
        """
        pass


class ManagementInterface(BaseInterface):
    """Interface for management related actions."""
    interface_type = 'management'

    @abc.abstractmethod
    def get_supported_boot_devices(self, task):
        """Get a list of the supported boot devices.

        :param task: A task from TaskManager.
        :returns: A list with the supported boot devices defined
                  in :mod:`ironic.common.boot_devices`.
        """

    @abc.abstractmethod
    def set_boot_device(self, task, device, persistent=False):
        """Set the boot device for a node.

        Set the boot device to use on next reboot of the node.

        :param task: A task from TaskManager.
        :param device: The boot device, one of
                       :mod:`ironic.common.boot_devices`.
        :param persistent: Boolean value. True if the boot device will
                           persist to all future boots, False if not.
                           Default: False.
        :raises: InvalidParameterValue if an invalid boot device is
                 specified.
        :raises: MissingParameterValue if a required parameter is missing
        """

    @abc.abstractmethod
    def get_boot_device(self, task):
        """Get the current boot device for a node.

        Provides the current boot device of the node. Be aware that not
        all drivers support this.

        :param task: A task from TaskManager.
        :raises: MissingParameterValue if a required parameter is missing
        :returns: A dictionary containing:

            :boot_device:
                Ahe boot device, one of :mod:`ironic.common.boot_devices` or
                None if it is unknown.
            :persistent:
                Whether the boot device will persist to all future boots or
                not, None if it is unknown.

        """

    def get_supported_boot_modes(self, task):
        """Get a list of the supported boot modes.

        NOTE: Not all drivers support this method. Older hardware
              may not implement that.

        :param task: A task from TaskManager.
        :raises: UnsupportedDriverExtension if requested operation is
                 not supported by the driver
        :raises: DriverOperationError or its derivative in case
                 of driver runtime error.
        :raises: MissingParameterValue if a required parameter is missing
        :returns: A list with the supported boot modes defined
                  in :mod:`ironic.common.boot_modes`. If boot
                  mode support can't be determined, empty list
                  is returned.
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='get_supported_boot_modes')

    def set_boot_mode(self, task, mode):
        """Set the boot mode for a node.

        Set the boot mode to use on next reboot of the node.

        Drivers implementing this method are required to implement
        the `get_supported_boot_modes` method as well.

        NOTE: Not all drivers support this method. Hardware supporting only
            one boot mode may not implement that.

        :param task: A task from TaskManager.
        :param mode: The boot mode, one of
                     :mod:`ironic.common.boot_modes`.
        :raises: InvalidParameterValue if an invalid boot mode is
                 specified.
        :raises: MissingParameterValue if a required parameter is missing
        :raises: UnsupportedDriverExtension if requested operation is
                 not supported by the driver
        :raises: DriverOperationError or its derivative in case
                 of driver runtime error.
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='set_boot_mode')

    def get_boot_mode(self, task):
        """Get the current boot mode for a node.

        Provides the current boot mode of the node.

        NOTE: Not all drivers support this method. Older hardware
              may not implement that.

        :param task: A task from TaskManager.
        :raises: MissingParameterValue if a required parameter is missing
        :raises: DriverOperationError or its  derivative in case
                 of driver runtime error.
        :raises: UnsupportedDriverExtension if requested operation is
                 not supported by the driver
        :returns: The boot mode, one of :mod:`ironic.common.boot_mode` or
                  None if it is unknown.
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='get_boot_mode')

    def get_secure_boot_state(self, task):
        """Get the current secure boot state for the node.

        NOTE: Not all drivers support this method. Older hardware
              may not implement that.

        :param task: A task from TaskManager.
        :raises: MissingParameterValue if a required parameter is missing
        :raises: DriverOperationError or its derivative in case
                 of driver runtime error.
        :raises: UnsupportedDriverExtension if secure boot is
                 not supported by the driver or the hardware
        :returns: Boolean
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='get_secure_boot_state')

    def set_secure_boot_state(self, task, state):
        """Set the current secure boot state for the node.

        NOTE: Not all drivers support this method. Older hardware
              may not implement that.

        :param task: A task from TaskManager.
        :param state: A new state as a boolean.
        :raises: MissingParameterValue if a required parameter is missing
        :raises: DriverOperationError or its derivative in case
                 of driver runtime error.
        :raises: UnsupportedDriverExtension if secure boot is
                 not supported by the driver or the hardware
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='set_secure_boot_state')

    @abc.abstractmethod
    def get_sensors_data(self, task):
        """Get sensors data method.

        :param task: A TaskManager instance.
        :raises: FailedToGetSensorData when getting the sensor data fails.
        :raises: FailedToParseSensorData when parsing sensor data fails.
        :returns: Returns a consistent format dict of sensor data grouped by
                  sensor type, which can be processed by Ceilometer.
                  eg,

                  ::

                      {
                        'Sensor Type 1': {
                          'Sensor ID 1': {
                            'Sensor Reading': 'current value',
                            'key1': 'value1',
                            'key2': 'value2'
                          },
                          'Sensor ID 2': {
                            'Sensor Reading': 'current value',
                            'key1': 'value1',
                            'key2': 'value2'
                          }
                        },
                        'Sensor Type 2': {
                          'Sensor ID 3': {
                            'Sensor Reading': 'current value',
                            'key1': 'value1',
                            'key2': 'value2'
                          },
                          'Sensor ID 4': {
                            'Sensor Reading': 'current value',
                            'key1': 'value1',
                            'key2': 'value2'
                          }
                        }
                      }
        """

    def inject_nmi(self, task):
        """Inject NMI, Non Maskable Interrupt.

        Inject NMI (Non Maskable Interrupt) for a node immediately.

        :param task: A TaskManager instance containing the node to act on.
        :raises: UnsupportedDriverExtension
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='inject_nmi')

    def get_supported_indicators(self, task, component=None):
        """Get a map of the supported indicators (e.g. LEDs).

        :param task: A task from TaskManager.
        :param component: If not `None`, return indicator information
            for just this component, otherwise return indicators for
            all existing components.
        :returns: A dictionary of hardware components
            (:mod:`ironic.common.components`) as keys with values
            being dictionaries having indicator IDs as keys and indicator
            properties as values.

            ::

                {
                    'chassis': {
                        'enclosure-0': {
                            "readonly": true,
                            "states": [
                                "off",
                                "on"
                            ]
                        }
                    },
                    'system':
                        'blade-A': {
                            "readonly": true,
                            "states": [
                                "pff",
                                "on"
                            ]
                        }
                    },
                    'drive':
                        'ssd0': {
                            "readonly": true,
                            "states": [
                                "off",
                                "on"
                            ]
                        }
                    }
                }

        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='get_supported_indicators')

    def set_indicator_state(self, task, component, indicator, state):
        """Set indicator on the hardware component to the desired state.

        :param task: A task from TaskManager.
        :param component: The hardware component, one of
            :mod:`ironic.common.components`.
        :param indicator: Indicator ID (as reported by
            `get_supported_indicators`).
        :state: Desired state of the indicator, one of
            :mod:`ironic.common.indicator_states`.
        :raises: InvalidParameterValue if an invalid component, indicator
            or state is specified.
        :raises: MissingParameterValue if a required parameter is missing
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='set_indicator_state')

    def get_indicator_state(self, task, component, indicator):
        """Get current state of the indicator of the hardware component.

        :param task: A task from TaskManager.
        :param component: The hardware component, one of
            :mod:`ironic.common.components`.
        :param indicator: Indicator ID (as reported by
            `get_supported_indicators`).
        :raises: InvalidParameterValue if an invalid component or indicator
            is specified.
        :raises: MissingParameterValue if a required parameter is missing
        :returns: Current state of the indicator, one of
            :mod:`ironic.common.indicator_states`.

        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='get_indicator_state')

    def detect_vendor(self, task):
        """Detects, stores, and returns the hardware vendor.

        If the Node object ``properties`` field does not already contain
        a ``vendor`` field, then this method is intended to query
        Detects the BMC hardware vendor and stores the returned value
        with-in the Node object ``properties`` field if detected.

        :param task: A task from TaskManager.
        :raises: InvalidParameterValue if an invalid component, indicator
            or state is specified.
        :raises: MissingParameterValue if a required parameter is missing
        :returns: String representing the BMC reported Vendor or
                  Manufacturer, otherwise returns None.
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='detect_vendor')

    def get_mac_addresses(self, task):
        """Get MAC address information for the node.

        :param task: A TaskManager instance containing the node to act on.
        :raises: UnsupportedDriverExtension
        :returns: A list of MAC addresses for the node

        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='get_mac_addresses')

    def get_virtual_media(self, task):
        """Get all virtual media devices from the node.

        :param task: A TaskManager instance containing the node to act on.
        :raises: UnsupportedDriverExtension

        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='get_virtual_media')

    def attach_virtual_media(self, task, device_type, image_url):
        """Attach a virtual media device to the node.

        :param task: A TaskManager instance containing the node to act on.
        :param device_type: Device type, one of
            :data:`ironic.common.boot_devices.VMEDIA_DEVICES`.
        :param image_url: URL of the image to attach, HTTP or HTTPS.
        :raises: UnsupportedDriverExtension

        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='attach_virtual_media')

    def detach_virtual_media(self, task, device_types=None):
        """Detach some or all virtual media devices from the node.

        :param task: A TaskManager instance containing the node to act on.
        :param device_types: A collection of device type, ones from
            :data:`ironic.common.boot_devices.VMEDIA_DEVICES`.
            If not provided, all devices are detached.
        :raises: UnsupportedDriverExtension

        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='detach_virtual_media')


class InspectInterface(BaseInterface):
    """Interface for inspection-related actions."""
    interface_type = 'inspect'

    ESSENTIAL_PROPERTIES = {'memory_mb', 'local_gb', 'cpu_arch'}
    """The properties required by scheduler/deploy."""

    @abc.abstractmethod
    def inspect_hardware(self, task):
        """Inspect hardware.

        Inspect hardware to obtain the essential & additional hardware
        properties.

        :param task: A task from TaskManager.
        :raises: HardwareInspectionFailure, if unable to get essential
                 hardware properties.
        :returns: Resulting state of the inspection i.e. states.MANAGEABLE
                  or None.
        """

    def abort(self, task):
        """Abort asynchronized hardware inspection.

        Abort an ongoing hardware introspection, this is only used for
        asynchronize based inspect interface.

        NOTE: This interface is called with node exclusive lock held, the
        interface implementation is expected to be a quick processing.

        :param task: a task from TaskManager.
        :raises: UnsupportedDriverExtension, if the method is not implemented
                 by specific inspect interface.
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='abort')

    def continue_inspection(self, task, inventory, plugin_data=None):
        """Continue in-band hardware inspection.

        Should not be implemented for purely out-of-band implementations.

        :param task: a task from TaskManager.
        :param inventory: hardware inventory from the node.
        :param plugin_data: optional plugin-specific data.
        :raises: UnsupportedDriverExtension, if the method is not implemented
                 by specific inspect interface.
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='abort')


def cache_bios_settings(func):
    """A decorator to cache bios settings after running the function.

    :param func: Function or method to wrap.
    """
    @functools.wraps(func)
    def wrapped(self, task, *args, **kwargs):
        result = func(self, task, *args, **kwargs)
        self.cache_bios_settings(task)
        return result
    return wrapped


class BIOSInterface(BaseInterface):
    interface_type = 'bios'

    @abc.abstractmethod
    def apply_configuration(self, task, settings):
        """Validate & apply BIOS settings on the given node.

        This method takes the BIOS settings from the settings param and
        applies BIOS settings on the given node. It may also validate the
        given bios settings before applying any settings and manage
        failures when setting an invalid BIOS config. In the case of
        needing password to update the BIOS config, it will be taken from
        the driver_info properties. After the BIOS configuration is done,
        cache_bios_settings will be called to update the node's BIOS setting
        table with the BIOS configuration applied on the node.

        :param task: a TaskManager instance.
        :param settings: Dictionary containing the BIOS configuration.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support BIOS configuration.
        :raises: InvalidParameterValue, if validation of settings fails.
        :raises: MissingParameterValue, if some required parameters are
            missing.
        :returns: states.CLEANWAIT if BIOS configuration is in progress
            asynchronously or None if it is complete.
        """

    @abc.abstractmethod
    def factory_reset(self, task):
        """Reset BIOS configuration to factory default on the given node.

        This method resets BIOS configuration to factory default on the
        given node. After the BIOS reset action is done, cache_bios_settings
        will be called to update the node's BIOS settings table with default
        bios settings.

        :param task: a TaskManager instance.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support BIOS reset.
        :returns: states.CLEANWAIT if BIOS configuration is in progress
            asynchronously or None if it is complete.
        """

    @abc.abstractmethod
    def cache_bios_settings(self, task):
        """Store or update BIOS properties on the given node.

        This method stores BIOS properties to the bios_settings table during
        'cleaning' operation and updates bios_settings table when
        apply_configuration() and factory_reset() are called to set new BIOS
        configurations. It will also update the timestamp of each bios setting.

        :param task: a TaskManager instance.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support getting BIOS properties from bare metal.
        :returns: None.
        """


class RAIDInterface(BaseInterface):
    interface_type = 'raid'

    def __init__(self):
        """Constructor for RAIDInterface class."""
        with open(RAID_CONFIG_SCHEMA, 'r') as raid_schema_fobj:
            self.raid_schema = json.load(raid_schema_fobj)

    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """
        return {}

    def validate(self, task):
        """Validates the RAID Interface.

        This method validates the properties defined by Ironic for RAID
        configuration. Driver implementations of this interface can override
        this method for doing more validations (such as BMC's credentials).

        :param task: A TaskManager instance.
        :raises: InvalidParameterValue, if the RAID configuration is invalid.
        :raises: MissingParameterValue, if some parameters are missing.
        """
        target_raid_config = task.node.target_raid_config
        if not target_raid_config:
            return
        self.validate_raid_config(task, target_raid_config)

    def validate_raid_config(self, task, raid_config):
        """Validates the given RAID configuration.

        This method validates the given RAID configuration.  Driver
        implementations of this interface can override this method to support
        custom parameters for RAID configuration.

        :param task: A TaskManager instance.
        :param raid_config: The RAID configuration to validate.
        :raises: InvalidParameterValue, if the RAID configuration is invalid.
        """
        raid.validate_configuration(raid_config, self.raid_schema)

    # NOTE(mgoddard): This is not marked as a deploy step, because it requires
    # the create_configuration method to support use during deployment, which
    # might not be true for all implementations. Subclasses wishing to expose
    # an apply_configuration deploy step should implement this method with a
    # deploy_step decorator. The RAID_APPLY_CONFIGURATION_ARGSINFO variable may
    # be used for the deploy_step argsinfo argument. The create_configuration
    # method must also accept a delete_existing argument.
    def apply_configuration(self, task, raid_config, create_root_volume=True,
                            create_nonroot_volumes=True,
                            delete_existing=True):
        """Applies RAID configuration on the given node.

        :param task: A TaskManager instance.
        :param raid_config: The RAID configuration to apply.
        :param create_root_volume: Setting this to False indicates
            not to create root volume that is specified in raid_config.
            Default value is True.
        :param create_nonroot_volumes: Setting this to False indicates
            not to create non-root volumes (all except the root volume) in
            raid_config.  Default value is True.
        :param delete_existing: Setting this to True indicates to delete RAID
            configuration prior to creating the new configuration.
        :raises: InvalidParameterValue, if the RAID configuration is invalid.
        :returns: states.DEPLOYWAIT if RAID configuration is in progress
            asynchronously or None if it is complete.
        """
        self.validate_raid_config(task, raid_config)
        node = task.node
        node.target_raid_config = raid_config
        node.save()
        return self.create_configuration(
            task,
            create_root_volume=create_root_volume,
            create_nonroot_volumes=create_nonroot_volumes,
            delete_existing=delete_existing)

    @abc.abstractmethod
    def create_configuration(self, task,
                             create_root_volume=True,
                             create_nonroot_volumes=True,
                             delete_existing=True):
        """Creates RAID configuration on the given node.

        This method creates a RAID configuration on the given node.
        It assumes that the target RAID configuration is already
        available in node.target_raid_config.
        Implementations of this interface are supposed to read the
        RAID configuration from node.target_raid_config. After the
        RAID configuration is done (either in this method OR in a call-back
        method), ironic.common.raid.update_raid_info()
        may be called to sync the node's RAID-related information with the
        RAID configuration applied on the node.

        :param task: A TaskManager instance.
        :param create_root_volume: Setting this to False indicates
            not to create root volume that is specified in the node's
            target_raid_config. Default value is True.
        :param create_nonroot_volumes: Setting this to False indicates
            not to create non-root volumes (all except the root volume) in the
            node's target_raid_config.  Default value is True.
        :param delete_existing: Setting this to True indicates to delete RAID
            configuration prior to creating the new configuration.
        :returns: states.CLEANWAIT (cleaning) or states.DEPLOYWAIT (deployment)
            if RAID configuration is in progress asynchronously, or None if it
            is complete.
        """

    @abc.abstractmethod
    def delete_configuration(self, task):
        """Deletes RAID configuration on the given node.

        This method deletes the RAID configuration on the give node.
        After RAID configuration is deleted, node.raid_config should be
        cleared by the implementation.

        :param task: A TaskManager instance.
        :returns: states.CLEANWAIT (cleaning) or states.DEPLOYWAIT (deployment)
            if deletion is in progress asynchronously, or None if it is
            complete.
        """

    def get_logical_disk_properties(self):
        """Get the properties that can be specified for logical disks.

        This method returns a dictionary containing the properties that can
        be specified for logical disks and a textual description for them.

        :returns: A dictionary containing properties that can be mentioned for
            logical disks and a textual description for them.
        """
        return raid.get_logical_disk_properties(self.raid_schema)


class NetworkInterface(BaseInterface):
    """Base class for network interfaces."""

    interface_type = 'network'

    # By default, no base network interface should suggest
    # any capability for other code to key off of to understand
    # if the requested instance has attributes which may trigger
    # additional code paths.
    capabilities = []

    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """
        return {}

    def validate(self, task):
        """Validates the network interface.

        :param task: A TaskManager instance.
        :raises: InvalidParameterValue, if the network interface configuration
            is invalid.
        :raises: MissingParameterValue, if some parameters are missing.
        """

    @abc.abstractmethod
    def port_changed(self, task, port_obj):
        """Handle any actions required when a port changes

        :param task: A TaskManager instance.
        :param port_obj: a changed Port object.
        :raises: Conflict, FailedToUpdateDHCPOptOnPort
        """

    @abc.abstractmethod
    def portgroup_changed(self, task, portgroup_obj):
        """Handle any actions required when a port changes

        :param task: A TaskManager instance.
        :param portgroup_obj: a changed Port object.
        :raises: Conflict, FailedToUpdateDHCPOptOnPort
        """

    @abc.abstractmethod
    def vif_attach(self, task, vif_info):
        """Attach a virtual network interface to a node

        :param task: A TaskManager instance.
        :param vif_info: a dictionary of information about a VIF.
            It must have an 'id' key, whose value is a unique identifier
            for that VIF.
        :raises: NetworkError, VifAlreadyAttached, NoFreePhysicalPorts
        """

    @abc.abstractmethod
    def vif_detach(self, task, vif_id):
        """Detach a virtual network interface from a node

        :param task: A TaskManager instance.
        :param vif_id: A VIF ID to detach
        :raises: NetworkError, VifNotAttached
        """

    @abc.abstractmethod
    def vif_list(self, task):
        """List attached VIF IDs for a node

        :param task: A TaskManager instance.
        :returns: List of VIF dictionaries, each dictionary will have an 'id'
            entry with the ID of the VIF.
        """

    @abc.abstractmethod
    def get_current_vif(self, task, p_obj):
        """Returns the currently used VIF associated with port or portgroup

        We are booting the node only in one network at a time, and presence of
        cleaning_vif_port_id means we're doing cleaning,
        of provisioning_vif_port_id - provisioning,
        of rescuing_vif_port_id - rescuing.
        Otherwise it's a tenant network.

        :param task: A TaskManager instance.
        :param p_obj: Ironic port or portgroup object.
        :returns: VIF ID associated with p_obj or None.
        """

    @abc.abstractmethod
    def add_provisioning_network(self, task):
        """Add the provisioning network to a node.

        :param task: A TaskManager instance.
        :raises: NetworkError
        """

    @abc.abstractmethod
    def remove_provisioning_network(self, task):
        """Remove the provisioning network from a node.

        :param task: A TaskManager instance.
        """

    @abc.abstractmethod
    def configure_tenant_networks(self, task):
        """Configure tenant networks for a node.

        :param task: A TaskManager instance.
        :raises: NetworkError
        """

    @abc.abstractmethod
    def unconfigure_tenant_networks(self, task):
        """Unconfigure tenant networks for a node.

        :param task: A TaskManager instance.
        """

    @abc.abstractmethod
    def add_cleaning_network(self, task):
        """Add the cleaning network to a node.

        :param task: A TaskManager instance.
        :returns: a dictionary in the form {port.uuid: neutron_port['id']}
        :raises: NetworkError
        """

    @abc.abstractmethod
    def remove_cleaning_network(self, task):
        """Remove the cleaning network from a node.

        :param task: A TaskManager instance.
        :raises: NetworkError
        """

    def validate_rescue(self, task):
        """Validates the network interface for rescue operation.

        :param task: A TaskManager instance.
        :raises: InvalidParameterValue, if the network interface configuration
            is invalid.
        :raises: MissingParameterValue, if some parameters are missing.
        """
        pass

    def add_rescuing_network(self, task):
        """Add the rescuing network to the node.

        :param task: A TaskManager instance.
        :returns: a dictionary in the form {port.uuid: neutron_port['id']}
        :raises: NetworkError
        :raises: InvalidParameterValue, if the network interface configuration
            is invalid.
        """
        return {}

    def remove_rescuing_network(self, task):
        """Removes the rescuing network from a node.

        :param task: A TaskManager instance.
        :raises: NetworkError
        :raises: InvalidParameterValue, if the network interface configuration
            is invalid.
        :raises: MissingParameterValue, if some parameters are missing.
        """
        pass

    def validate_inspection(self, task):
        """Validate that the node has required properties for inspection.

        :param task: A TaskManager instance with the node being checked
        :raises: MissingParameterValue if node is missing one or more required
            parameters
        :raises: UnsupportedDriverExtension
        """
        raise exception.UnsupportedDriverExtension(
            driver=task.node.driver, extension='validate_inspection')

    def add_inspection_network(self, task):
        """Add the inspection network to the node.

        :param task: A TaskManager instance.
        :returns: a dictionary in the form {port.uuid: neutron_port['id']}
        :raises: NetworkError
        :raises: InvalidParameterValue, if the network interface configuration
            is invalid.
        """
        return {}

    def remove_inspection_network(self, task):
        """Removes the inspection network from a node.

        :param task: A TaskManager instance.
        :raises: NetworkError
        :raises: InvalidParameterValue, if the network interface configuration
            is invalid.
        :raises: MissingParameterValue, if some parameters are missing.
        """

    def need_power_on(self, task):
        """Check if node must be powered on before applying network changes

        :param task: A TaskManager instance.
        :returns: Boolean.
        """
        return False

    def get_node_network_data(self, task):
        """Return network configuration for node NICs.

        Gather L2 and L3 network settings from ironic port/portgroups
        objects and underlying network provider, then put together
        collected data in form of Nova network metadata (`network_data.json`)
        dict.

        Ironic would eventually pass network configuration to the node
        being managed out-of-band.

        :param task: A TaskManager instance.
        :raises: InvalidParameterValue, if the network interface configuration
            is invalid.
        :raises: MissingParameterValue, if some parameters are missing.
        :returns: a dict holding network configuration information adhering
            Nova network metadata layout (`network_data.json`).
        """
        return task.node.network_data or {}

    def add_servicing_network(self, task):
        """Add the servicing network to the node.

        :param task: A TaskManager instance.
        :returns: a dictionary in the form {port.uuid: neutron_port['id']}
        :raises: NetworkError
        :raises: InvalidParameterValue, if the network interface configuration
            is invalid.
        """
        return {}

    def remove_servicing_network(self, task):
        """Removes the servicing network from a node.

        :param task: A TaskManager instance.
        :raises: NetworkError
        :raises: InvalidParameterValue, if the network interface configuration
            is invalid.
        :raises: MissingParameterValue, if some parameters are missing.
        """
        pass


class StorageInterface(BaseInterface, metaclass=abc.ABCMeta):
    """Base class for storage interfaces."""

    interface_type = 'storage'

    @abc.abstractmethod
    def attach_volumes(self, task):
        """Informs the storage subsystem to attach all volumes for the node.

        :param task: A TaskManager instance.
        :raises: UnsupportedDriverExtension
        """

    @abc.abstractmethod
    def detach_volumes(self, task):
        """Informs the storage subsystem to detach all volumes for the node.

        :param task: A TaskManager instance.
        :raises: UnsupportedDriverExtension
        """

    @abc.abstractmethod
    def should_write_image(self, task):
        """Determines if deploy should perform the image write-out.

        :param task: A TaskManager instance.
        :returns: Boolean value to indicate if the interface expects
                  the image to be written by Ironic.
        :raises: UnsupportedDriverExtension
        """


def cache_firmware_components(func):
    """A decorator to cache firmware components after running the function.

    :param func: Function or method to wrap.
    """
    @functools.wraps(func)
    def wrapped(self, task, *args, **kwargs):
        result = func(self, task, *args, **kwargs)
        self.cache_firmware_components(task)
        return result
    return wrapped


class FirmwareInterface(BaseInterface):
    """Base class for firmware interface"""

    interface_type = 'firmware'

    @abc.abstractmethod
    def update(self, task, settings):
        """Update the Firmware on the given using the settings for components.

        :param task: a TaskManager instance.
        :param settings: a list of dictionaries, each dictionary contains the
            component name and the url that will be used to update the
            firmware.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support update via the interface.
        :raises: InvalidParameterValue, if validation of the settings fails.
        :raises: MissingParamterValue, if some required parameters are
            missing.
        :returns: states.CLEANWAIT if Firmware update with the settings is in
            progress asynchronously of None if it is complete.
        """

    @abc.abstractmethod
    def cache_firmware_components(self, task):
        """Store or update Firmware Components on the given node.

        This method stores Firmware Components to the firmware_information
        table during 'cleaning' operation. It will also update the timestamp
        of each Firmware Component.

        :param task: a TaskManager instance.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support getting Firmware Components from bare metal.
        """


def _validate_argsinfo(argsinfo):
    """Validate args info.

    This method validates args info, so that the values are the expected
    data types and required values are specified.

    :param argsinfo: a dictionary of keyword arguments where key is the name of
        the argument and value is a dictionary as follows::

            'description': <description>. Required. This should include
                           possible values.
            'required': Boolean. Optional; default is False. True if this
                        argument is required.  If so, it must be specified in
                        the clean request; false if it is optional.
    :raises InvalidParameterValue: if any of the arguments are invalid
    """
    if not argsinfo:
        return

    if not isinstance(argsinfo, dict):
        raise exception.InvalidParameterValue(
            _('"argsinfo" must be a dictionary instead of "%s"') %
            argsinfo)
    for (arg, info) in argsinfo.items():
        if not isinstance(info, dict):
            raise exception.InvalidParameterValue(
                _('Argument "%(arg)s" must be a dictionary instead of '
                  '"%(val)s".') % {'arg': arg, 'val': info})
        has_description = False
        for (key, value) in info.items():
            if key == 'description':
                if not isinstance(value, str):
                    raise exception.InvalidParameterValue(
                        _('For argument "%(arg)s", "description" must be a '
                          'string value instead of "%(value)s".') %
                        {'arg': arg, 'value': value})
                has_description = True
            elif key == 'required':
                if not isinstance(value, bool):
                    raise exception.InvalidParameterValue(
                        _('For argument "%(arg)s", "required" must be a '
                          'Boolean value instead of "%(value)s".') %
                        {'arg': arg, 'value': value})
            else:
                raise exception.InvalidParameterValue(
                    _('Argument "%(arg)s" has an invalid key named "%(key)s". '
                      'It must be "description" or "required".')
                    % {'key': key, 'arg': arg})
        if not has_description:
            raise exception.InvalidParameterValue(
                _('Argument "%(arg)s" is missing a "description".') %
                {'arg': arg})


def clean_step(priority, abortable=False, argsinfo=None,
               requires_ramdisk=True):
    """Decorator for cleaning steps.

    Cleaning steps may be used in manual or automated cleaning.

    For automated cleaning, only steps with priorities greater than 0 are
    used. These steps are ordered by priority from highest value to lowest
    value. For steps with the same priority, they are ordered by driver
    interface priority (see conductor.steps.CLEANING_INTERFACE_PRIORITY).
    execute_clean_step() will be called on each step.

    For manual cleaning, the clean steps will be executed in a similar fashion
    to automated cleaning, but the steps and order of execution must be
    explicitly specified by the user when invoking the cleaning API.

    Decorated clean steps must take as the only positional argument, a
    TaskManager object. Clean steps used in manual cleaning may also take
    keyword variable arguments (as described in argsinfo).

    Clean steps can be either synchronous or asynchronous.  If the step is
    synchronous, it should return `None` when finished, and the conductor
    will continue on to the next step. While the clean step is executing, the
    node will be in `states.CLEANING` provision state. If the step is
    asynchronous, the step should return `states.CLEANWAIT` to the
    conductor before it starts the asynchronous work.  When the step is
    complete, the step should make an RPC call to `continue_node_clean` to
    move to the next step in cleaning. The node will be in `states.CLEANWAIT`
    provision state during the asynchronous work.

    Examples::

        class MyInterface(base.BaseInterface):
            # CONF.example_cleaning_priority should be an int CONF option
            @base.clean_step(priority=CONF.example_cleaning_priority)
            def example_cleaning(self, task):
                # do some cleaning

            @base.clean_step(priority=0, abortable=True, argsinfo=
                             {'size': {'description': 'size of widget (MB)',
                                       'required': True}})
            def advanced_clean(self, task, **kwargs):
                # do some advanced cleaning

    :param priority: an integer priority, should be a CONF option
    :param abortable: Boolean value. Whether the clean step is abortable
        or not; defaults to False.
    :param argsinfo: a dictionary of keyword arguments where key is the name of
        the argument and value is a dictionary as follows::

            'description': <description>. Required. This should include
                           possible values.
            'required': Boolean. Optional; default is False. True if this
                        argument is required.  If so, it must be specified in
                        the clean request; false if it is optional.
    :param requires_ramdisk: Whether this step requires the ramdisk
        to be running. Should be set to False for purely out-of-band steps.
    :raises InvalidParameterValue: if any of the arguments are invalid
    """
    def decorator(func):
        func._is_clean_step = True
        if isinstance(priority, int):
            func._clean_step_priority = priority
        else:
            raise exception.InvalidParameterValue(
                _('"priority" must be an integer value instead of "%s"')
                % priority)

        if isinstance(abortable, bool):
            func._clean_step_abortable = abortable
        else:
            raise exception.InvalidParameterValue(
                _('"abortable" must be a Boolean value instead of "%s"')
                % abortable)

        _validate_argsinfo(argsinfo)
        func._clean_step_argsinfo = argsinfo
        func._clean_step_requires_ramdisk = requires_ramdisk
        return func
    return decorator


def deploy_step(priority, argsinfo=None):
    """Decorator for deployment steps.

    Only steps with priorities greater than 0 are used.
    These steps are ordered by priority from highest value to lowest
    value. For steps with the same priority, they are ordered by driver
    interface priority (see conductor.steps.DEPLOYING_INTERFACE_PRIORITY).
    execute_deploy_step() will be called on each step.

    Decorated deploy steps must take as the only positional argument, a
    TaskManager object.

    Deploy steps can be either synchronous or asynchronous.  If the step is
    synchronous, it should return `None` when finished, and the conductor
    will continue on to the next step. While the deploy step is executing, the
    node will be in `states.DEPLOYING` provision state. If the step is
    asynchronous, the step should return `states.DEPLOYWAIT` to the
    conductor before it starts the asynchronous work.  When the step is
    complete, the step should make an RPC call to `continue_node_deploy` to
    move to the next step in deployment. The node will be in
    `states.DEPLOYWAIT` provision state during the asynchronous work.

    Examples::

        class MyInterface(base.BaseInterface):
            @base.deploy_step(priority=100)
            def example_deploying(self, task):
                # do some deploying

    :param priority: an integer (>=0) priority; used for determining the order
        in which the step is run in the deployment process.
    :param argsinfo: a dictionary of keyword arguments where key is the name of
        the argument and value is a dictionary as follows::

            'description': <description>. Required. This should include
                           possible values.
            'required': Boolean. Optional; default is False. True if this
                        argument is required.  If so, it must be specified in
                        the deployment request; false if it is optional.
    :raises InvalidParameterValue: if any of the arguments are invalid
    """
    def decorator(func):
        func._is_deploy_step = True
        if isinstance(priority, int) and priority >= 0:
            func._deploy_step_priority = priority
        else:
            raise exception.InvalidParameterValue(
                _('"priority" must be an integer value >= 0, instead of "%s"')
                % priority)

        _validate_argsinfo(argsinfo)
        func._deploy_step_argsinfo = argsinfo
        return func
    return decorator


def verify_step(priority):
    """Decorator for verify steps.

    Only steps with priorities greater than 0 are used. These steps are
    ordered by priority from highest value to lowest value.
    For steps with the same priority, they are ordered by driver
    interface priority (see conductor.steps.VERIFY_INTERFACE_PRIORITY).
    execute_verify_step() will be called on each step.

    Decorated verify steps must take as the only positional argument, a
    TaskManager object.

    Verify steps are synchronous and should return `None` when finished,
    and the conductor will continue on to the next step. While the verify step
    is executing, the node will be in `states.VERIFYING` provision state.

    Examples::

        class MyInterface(base.BaseInterface):
            @base.verify_step(priority=100)
            def example_verifying(self, task):
                # do some verifying

    :param priority: an integer (>=0) priority; used for determining the order
        in which the step is run in the verification process.

    :raises InvalidParameterValue: if any of the arguments are invalid
    """
    def decorator(func):
        func._is_verify_step = True
        if isinstance(priority, int) and priority >= 0:
            func._verify_step_priority = priority
        else:
            raise exception.InvalidParameterValue(
                _('"priority" must be an integer value >= 0, instead of "%s"')
                % priority)

        return func
    return decorator


def service_step(priority=None, abortable=False, argsinfo=None,
                 requires_ramdisk=True):
    """Decorator for service steps.

    Service steps may be used in performing service upon a node.

    For service, the steps will be executed in a similar fashion
    to cleaning, but the steps and order of execution must be
    explicitly specified by the user when invoking the servicing API.

    Decorated service steps must take as the only a single positional
    argument, a TaskManager object, in addition to a keyword arguments
    variable (as described in argsinfo).

    Service steps can be either synchronous or asynchronous.  If the step is
    synchronous, it should return `None` when finished, and the conductor
    will continue on to the next step. While the clean step is executing, the
    node will be in `states.SERVICING` provision state. If the step is
    asynchronous, the step should return `states.SERVICEWAIT` to the
    conductor before it starts the asynchronous work.  When the step is
    complete, the step should make an RPC call to `continue_node_service` to
    move to the next step in servicing. The node will be in
    `states.SERVICEWAIT` provision state during the asynchronous work.

    Examples::

        class MyInterface(base.BaseInterface):
            @base.service_step()
            def example_service(self, task):
                # do some service actions

            @base.service_step(priority=0, abortable=True, argsinfo=
                               {'size': {'description': 'size of widget (MB)',
                                         'required': True}})
            def advanced_service(self, task, **kwargs):
                # do some advanced magical service

    :param priority: an integer priority, defaults to None which maps to 0.
                     Priorities are not considered, by default but exists
                     should this functionality be adopted later on to align
                     with the steps framework.
    :param abortable: Boolean value. Whether the clean step is abortable
        or not; defaults to False.
    :param argsinfo: a dictionary of keyword arguments where key is the name of
        the argument and value is a dictionary as follows::

            'description': <description>. Required. This should include
                           possible values.
            'required': Boolean. Optional; default is False. True if this
                        argument is required.  If so, it must be specified in
                        the service request; false if it is optional.
    :param requires_ramdisk: Whether this step requires the ramdisk
        to be running. Should be set to False for purely out-of-band steps.
    :raises InvalidParameterValue: if any of the arguments are invalid
    """
    def decorator(func):
        func._is_service_step = True
        if isinstance(priority, int):
            func._service_step_priority = priority
        else:
            # Service steps are only invoked by operators in a model
            # like manual cleaning, so there is no need to explicitly
            # require it on the decorator.
            func._service_step_priority = 0

        if isinstance(abortable, bool):
            func._service_step_abortable = abortable
        else:
            raise exception.InvalidParameterValue(
                _('"abortable" must be a Boolean value instead of "%s"')
                % abortable)

        _validate_argsinfo(argsinfo)
        func._service_step_argsinfo = argsinfo
        func._service_step_requires_ramdisk = requires_ramdisk
        return func
    return decorator
