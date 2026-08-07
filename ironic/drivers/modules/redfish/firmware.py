#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import time
from urllib.parse import urlparse

from oslo_log import log
from oslo_utils import timeutils
import sushy

from ironic.common import async_steps
from ironic.common import exception
from ironic.common.i18n import _
from ironic.common import metrics_utils
from ironic.common import states
from ironic.conductor import periodics
from ironic.conductor import utils as manager_utils
from ironic.conf import CONF
from ironic.drivers import base
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules.redfish import firmware_utils
from ironic.drivers.modules.redfish import utils as redfish_utils
from ironic import objects

LOG = log.getLogger(__name__)

METRICS = metrics_utils.get_metrics_logger(__name__)

# Temporary field names stored in node.driver_internal_info
BMC_FW_VERSION_BEFORE_UPDATE = 'bmc_fw_version_before_update'
FIRMWARE_REBOOT_REQUESTED = 'firmware_reboot_requested'
FIRMWARE_BATCHED_UPDATE = 'firmware_batched_update'
FIRMWARE_BATCH_SUBMITTED = 'firmware_batch_submitted'
FIRMWARE_BATCH_CURRENT_INDEX = 'firmware_batch_current_index'
FIRMWARE_BATCH_REBOOT_TIME = 'firmware_batch_reboot_time'
FIRMWARE_ALLOW_GROUPING = 'firmware_allow_grouping'

# Temporary field names stored in fw_upd/current_update settings dict
BMC_UPDATE_COMPLETED = 'bmc_update_completed'


def _leading_batchable_run(settings, max_size=None):
    """Length of the leading run of adjacent non-BMC components.

    A reboot is shared by a maximal run of adjacent non-BMC components.
    This function returns the length of that leading run, optionally
    capped to max_size (used for batch-of-1 when grouping is disabled).

    :param settings: list of firmware update dicts
    :param max_size: optional upper bound on the returned run length
    :returns: int — number of components in the leading batchable run
    """
    for i, s in enumerate(settings):
        if max_size is not None and i >= max_size:
            return max_size
        component = s.get('component', '')
        if redfish_utils.get_component_type(component) == redfish_utils.BMC:
            return i
    if max_size is not None:
        return min(len(settings), max_size)
    return len(settings)


class RedfishFirmware(base.FirmwareInterface):

    _FW_SETTINGS_ARGSINFO = {
        'settings': {
            'description': (
                'A list of dicts with firmware components to be updated. '
                'The per-component wait argument is only supported for '
                'BMC components; specifying it on a non-BMC component '
                'is rejected.'
            ),
            'required': True
        },
        'allow_grouping_reboots': {
            'description': (
                'Boolean. When True, adjacent non-BMC firmware updates '
                'share a single consolidated host reboot instead of '
                'rebooting after each component. BMC entries segment the '
                'list into independent phases. Ironic does not reorder '
                'the settings list. Duplicate component values are '
                'rejected. Defaults to False.'
            ),
            'required': False
        }
    }

    def _batch_run_length(self, node, settings):
        """Leading batchable run length, respecting the grouping mode.

        :param node: the Ironic node object
        :param settings: list of firmware update dicts
        :returns: int — capped to 1 when grouping is disabled
        """
        if node.driver_internal_info.get(FIRMWARE_ALLOW_GROUPING):
            return _leading_batchable_run(settings)
        return _leading_batchable_run(settings, max_size=1)

    def _staged_pending(self, node, settings, exclude=None):
        """Components the BMC accepted but has not yet applied.

        Derived, not stored: 'task_monitor' is set only on a successful
        SimpleUpdate, and FIRMWARE_BATCH_SUBMITTED marks the point after
        which the consolidated reboot has been issued.

        :param node: the Ironic node object
        :param settings: list of firmware update dicts
        :param exclude: a settings dict to omit (the one that just failed)
        :returns: list of component name strings, possibly empty
        """
        if node.driver_internal_info.get(FIRMWARE_BATCH_SUBMITTED):
            return []
        run_length = self._batch_run_length(node, settings)
        return [s.get('component', '') for s in settings[:run_length]
                if s.get('task_monitor') and s is not exclude]

    def _staged_pending_note(self, node, settings, exclude=None):
        """Operator-facing suffix naming components still armed on the BMC.

        :param node: the Ironic node object
        :param settings: list of firmware update dicts
        :param exclude: a settings dict to omit (the one that just failed)
        :returns: a string to append to an error message, or '' if nothing
            is currently staged and pending
        """
        pending = self._staged_pending(node, settings, exclude)
        if not pending:
            return ''
        LOG.warning('Firmware update failed for node %(node)s with '
                    'components already staged on the BMC: %(components)s. '
                    'These remain scheduled to apply on the next host boot.',
                    {'node': node.uuid, 'components': ', '.join(pending)})
        return _(' Components already staged on the BMC and still scheduled '
                 'to apply on the next host boot from any source: '
                 '%(components)s. Power-cycling this node will apply them, '
                 'and retrying firmware.update will stage them a second '
                 'time.') % {'components': ', '.join(pending)}

    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """
        return redfish_utils.COMMON_PROPERTIES.copy()

    def validate(self, task):
        """Validates the driver information needed by the redfish driver.

        :param task: a TaskManager instance containing the node to act on.
        :raises: InvalidParameterValue on malformed parameter(s)
        :raises: MissingParameterValue on missing parameter(s)
        """
        redfish_utils.parse_driver_info(task.node)

    @METRICS.timer('RedfishFirmware.cache_firmware_components')
    def cache_firmware_components(self, task):
        """Store or update Firmware Components on the given node.

        This method stores Firmware Components to the firmware_information
        table during 'cleaning' operation. It will also update the timestamp
        of each Firmware Component.

        :param task: a TaskManager instance.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support getting Firmware Components from bare metal.
        """

        node_id = task.node.id
        settings = []
        # NOTE(iurygregory): currently we will only retrieve BIOS and BMC
        # firmware information through the redfish system and manager.

        system = redfish_utils.get_system(task.node)

        if system.bios_version:
            bios_fw = {'component': redfish_utils.BIOS,
                       'current_version': system.bios_version}
            settings.append(bios_fw)
        else:
            LOG.debug('Could not retrieve BiosVersion in node %(node_uuid)s '
                      'system %(system)s', {'node_uuid': task.node.uuid,
                                            'system': system.identity})

        # NOTE(iurygregory): normally we only relay on the System to
        # perform actions, but to retrieve the BMC Firmware we need to
        # access the Manager.
        try:
            manager = redfish_utils.get_manager(task.node, system)
            if manager.firmware_version:
                bmc_fw = {'component': redfish_utils.BMC,
                          'current_version': manager.firmware_version}
                settings.append(bmc_fw)
            else:
                LOG.debug('Could not retrieve FirmwareVersion in node '
                          '%(node_uuid)s manager %(manager)s',
                          {'node_uuid': task.node.uuid,
                           'manager': manager.identity})
        except exception.RedfishError:
            LOG.warning('No manager available to retrieve Firmware '
                        'from the bmc of node %s', task.node.uuid)

        nic_components = None
        try:
            nic_components = self.retrieve_nic_components(task, system)
        except (exception.RedfishError,
                sushy.exceptions.BadRequestError,
                sushy.exceptions.MissingAttributeError) as e:
            # NOTE(janders) if an exception is raised, log a warning
            # with exception details. This is important for HP hardware
            # which at the time of writing this are known to return 400
            # responses to GET NetworkAdapters while OS isn't fully booted
            LOG.warning('Unable to access NetworkAdapters on node '
                        '%(node_uuid)s, Error: %(error)s',
                        {'node_uuid': task.node.uuid, 'error': e})
        # NOTE(janders) if no exception is raised but no NICs are returned,
        # state that clearly but in a lower severity message
        if nic_components == []:
            LOG.debug('Could not retrieve Firmware Package Version from '
                      'NetworkAdapters on node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
        elif nic_components:
            settings.extend(nic_components)

        if not settings:
            error_msg = (_('Cannot retrieve firmware for node %s: no '
                           'supported components') % task.node.uuid)
            LOG.error(error_msg)
            raise exception.UnsupportedDriverExtension(error_msg)

        create_list, update_list, nochange_list = (
            objects.FirmwareComponentList.sync_firmware_components(
                task.context, node_id, settings))

        if create_list:
            for new_fw in create_list:
                new_fw_cmp = objects.FirmwareComponent(
                    task.context,
                    node_id=node_id,
                    component=new_fw['component'],
                    current_version=new_fw['current_version']
                )
                new_fw_cmp.create()
        if update_list:
            for up_fw in update_list:
                up_fw_cmp = objects.FirmwareComponent.get(
                    task.context,
                    node_id=node_id,
                    name=up_fw['component']
                )
                up_fw_cmp.last_version_flashed = up_fw.get('current_version')
                up_fw_cmp.current_version = up_fw.get('current_version')
                up_fw_cmp.save()

    def retrieve_nic_components(self, task, system):
        """Helper function to retrieve all NICs components on a given node.

        :param task: a TaskManager instance.
        :param system: a Redfish System object
        :returns: a list of NIC components
        """
        nic_list = []
        try:
            chassis = redfish_utils.get_chassis(task.node, system)
        except exception.RedfishError:
            LOG.debug('No chassis available to retrieve NetworkAdapters '
                      'firmware information on node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
            return nic_list

        try:
            network_adapters = chassis.network_adapters
            if network_adapters is None:
                LOG.debug('NetworkAdapters not available on chassis for '
                          'node %(node_uuid)s',
                          {'node_uuid': task.node.uuid})
                return nic_list
            adapters = network_adapters.get_members()
        except sushy.exceptions.MissingAttributeError:
            LOG.debug('NetworkAdapters not available on chassis for '
                      'node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
            return nic_list

        for net_adp in adapters:
            for net_adp_ctrl in net_adp.controllers:
                fw_pkg_v = net_adp_ctrl.firmware_package_version
                if not fw_pkg_v:
                    continue

                if net_adp.serial_number:
                    net_adp_id = net_adp.serial_number
                    LOG.debug('Using SerialNumber %(serial_number)s for '
                              'NetworkAdapter %(net_adp_id)s',
                              {'serial_number': net_adp.serial_number,
                               'net_adp_id': net_adp.identity})
                else:
                    net_adp_id = net_adp.identity
                    LOG.debug('Using Identity %(identity)s for '
                              'NetworkAdapter %(net_adp_id)s',
                              {'identity': net_adp.identity,
                               'net_adp_id': net_adp.identity})

                net_adp_fw = {'component': redfish_utils.NIC_COMPONENT_PREFIX
                              + net_adp_id, 'current_version': fw_pkg_v}
                nic_list.append(net_adp_fw)

        return nic_list

    @METRICS.timer('RedfishFirmware.update')
    @base.deploy_step(priority=0, abortable=False,
                      argsinfo=_FW_SETTINGS_ARGSINFO)
    @base.clean_step(priority=0, abortable=False,
                     argsinfo=_FW_SETTINGS_ARGSINFO,
                     requires_ramdisk=True)
    @base.service_step(priority=0, abortable=False,
                       argsinfo=_FW_SETTINGS_ARGSINFO,
                       requires_ramdisk=False)
    def update(self, task, settings, allow_grouping_reboots=False):
        """Update the Firmware on the node using the settings for components.

        :param task: a TaskManager instance.
        :param settings: a list of dictionaries, each dictionary contains the
            component name and the url that will be used to update the
            firmware.
        :param allow_grouping_reboots: Boolean. When True, non-BMC firmware
            updates are batched into a single host reboot. Defaults to False.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support update via the interface.
        :raises: InvalidParameterValue, if validation of the settings fails.
        :raises: MissingParamterValue, if some required parameters are
            missing.
        :returns: states.CLEANWAIT if Firmware update with the settings is in
            progress asynchronously of None if it is complete.
        """
        firmware_utils.validate_firmware_interface_update_args(settings)
        if not isinstance(allow_grouping_reboots, bool):
            raise exception.InvalidParameterValue(
                _('allow_grouping_reboots must be a boolean, '
                  'got %s') % type(allow_grouping_reboots).__name__)
        if allow_grouping_reboots:
            seen = set()
            for s in settings:
                comp = s.get('component', '')
                if comp in seen:
                    raise exception.InvalidParameterValue(
                        _("component '%(comp)s' appears more than once; "
                          "batched updates require distinct components. "
                          "Use separate firmware.update steps, or omit "
                          "allow_grouping_reboots, for staged or sequential "
                          "updates of the same component.") % {'comp': comp})
                seen.add(comp)

        for s in settings:
            comp = s.get('component', '')
            if (s.get('wait')
                    and redfish_utils.get_component_type(comp)
                    != redfish_utils.BMC):
                raise exception.InvalidParameterValue(
                    _("per-component 'wait' is only supported for BMC "
                      "components. Remove 'wait' from component "
                      "'%(comp)s'.") % {'comp': comp})

        node = task.node
        update_service = redfish_utils.get_update_service(node)

        LOG.debug('Updating Firmware on node %(node_uuid)s with settings '
                  '%(settings)s, allow_grouping_reboots=%(group)s',
                  {'node_uuid': node.uuid, 'settings': settings,
                   'group': allow_grouping_reboots})

        node.set_driver_internal_info(FIRMWARE_BATCHED_UPDATE, True)
        node.set_driver_internal_info(FIRMWARE_ALLOW_GROUPING,
                                      allow_grouping_reboots)
        node.set_driver_internal_info(
            'redfish_fw_update_start_time',
            timeutils.utcnow().isoformat())

        run_length = self._batch_run_length(node, settings)
        if run_length > 0:
            self._execute_batched_non_bmc_updates(
                task, update_service, settings)
            return async_steps.get_return_state(node)

        # First component is BMC — use sequential path
        fw_upd = settings[0]
        self._submit_simple_update(node, update_service, fw_upd)
        node.set_driver_internal_info('redfish_fw_updates', settings)
        self._setup_bmc_update_monitoring(node, fw_upd)
        node.save()
        return async_steps.get_return_state(node)

    def _clean_temp_fields(self, node):
        """Clean up temporary fields used during firmware update monitoring.

        This ensures no stale data interferes with new firmware updates.

        :param node: the Ironic node object
        """
        # BMC-related temp fields
        node.del_driver_internal_info(BMC_FW_VERSION_BEFORE_UPDATE)
        # General firmware temp fields
        node.del_driver_internal_info(FIRMWARE_REBOOT_REQUESTED)

    def _setup_bmc_update_monitoring(self, node, fw_upd):
        """Set up monitoring for BMC firmware update.

        BMC updates do not reboot immediately. Instead, we check the BMC
        version periodically. If the version changed, we continue without
        reboot. If timeout expires without version change, we trigger a reboot.

        :param node: the Ironic node object
        :param fw_upd: firmware update settings dict
        """
        # Clean any stale temp fields from previous updates
        self._clean_temp_fields(node)

        # Record current BMC version before update
        try:
            system = redfish_utils.get_system(node)
            manager = redfish_utils.get_manager(node, system)
            current_bmc_version = manager.firmware_version
            node.set_driver_internal_info(
                BMC_FW_VERSION_BEFORE_UPDATE, current_bmc_version)
            LOG.debug('BMC version before update for node %(node)s: '
                      '%(version)s',
                      {'node': node.uuid, 'version': current_bmc_version})
        except Exception as e:
            LOG.warning('Could not read BMC version before update for '
                        'node %(node)s: %(error)s',
                        {'node': node.uuid, 'error': e})

        LOG.info('BMC firmware update for node %(node)s. '
                 'Monitoring BMC version instead of immediate reboot.',
                 {'node': node.uuid})

        # Use wait_interval or default reboot delay
        wait_interval = fw_upd.get('wait')
        if wait_interval is None:
            wait_interval = CONF.redfish.firmware_update_reboot_delay
        fw_upd['wait'] = wait_interval
        # Set wait_start_time for polling interval and bmc_check_start_time
        # for total timeout tracking (wait_start_time gets updated each poll)
        start_time = str(timeutils.utcnow().isoformat())
        fw_upd['wait_start_time'] = start_time
        fw_upd['bmc_check_start_time'] = start_time
        # Mark this as a BMC update so we can handle timeouts properly
        fw_upd['component_type'] = redfish_utils.BMC

        # BMC: Set async flags without immediate reboot
        deploy_utils.set_async_step_flags(
            node,
            reboot=False,
            polling=True
        )

    def _get_current_bmc_version(self, node):
        """Get current BMC firmware version.

        Note: BMC may be temporarily unresponsive after firmware update.
        Expected exceptions (timeouts, connection refused, HTTP errors) are
        caught and logged, returning None to indicate version unavailable.

        :param node: the Ironic node object
        :returns: Current BMC firmware version string, or None if BMC
                  is unresponsive/inaccessible
        """
        try:
            system = redfish_utils.get_system(node)
            manager = redfish_utils.get_manager(node, system)
            return manager.firmware_version
        except (exception.RedfishError,
                exception.RedfishConnectionError,
                sushy.exceptions.SushyError) as e:
            # BMC unresponsiveness is expected after firmware update
            # (timeouts, connection refused, HTTP 4xx/5xx errors)
            LOG.debug('BMC temporarily unresponsive for node %(node)s: '
                      '%(error)s', {'node': node.uuid, 'error': e})
            return None

    def _handle_bmc_update_completion(self, task, update_service,
                                      settings, current_update):
        """Handle BMC firmware update completion with version checking.

        For BMC updates, we don't reboot immediately. Instead, we check
        the BMC version periodically. If the version changed, we continue
        without reboot. If timeout expires without version change, we trigger
        a reboot.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        :param current_update: the current firmware update being processed
        """
        # Upgrade the lock to ensure we are using the latest info from
        # the node.
        task.upgrade_lock()
        node = task.node

        # Try to get current BMC version
        # Note: BMC may be unresponsive after firmware update - expected
        current_version = self._get_current_bmc_version(node)
        version_before = node.driver_internal_info.get(
            BMC_FW_VERSION_BEFORE_UPDATE)

        # If we can read the version and it changed, update is complete
        if (current_version is not None
                and version_before is not None
                and current_version != version_before):
            node.del_driver_internal_info(BMC_FW_VERSION_BEFORE_UPDATE)

            # Check if more components are pending updates after BMC update
            if len(settings) > 1:
                # Upgrade the lock to ensure we are using the latest info from
                # the node.
                task.upgrade_lock()
                # More components to update - trigger reboot before continuing
                #  Some hardware can only execute NIC firmware updates after
                # the host reboots following the BMC firmware update.

                LOG.info('BMC firmware update complete for node %(node)s. '
                         'More components pending - triggering reboot before '
                         'continuing to next component.',
                         {'node': node.uuid})
                # Set flag to indicate reboot completed, ready to continue
                # This ensures we reboot and continue with the next component
                # update, this is required because we saw cases where NIC
                # updates were not being executed after the BMC update.
                current_update[BMC_UPDATE_COMPLETED] = True
                node.set_driver_internal_info('redfish_fw_updates', settings)
                deploy_utils.set_async_step_flags(
                    node, reboot=True, polling=True)
                manager_utils.node_power_action(task, states.REBOOT)
                return
            else:
                # Last component - no reboot needed
                # Servicing/Cleaning will trigger one.
                LOG.info('BMC firmware version for node %(node)s changed '
                         'from %(old)s to %(new)s.  Update complete last '
                         'component',
                         {'node': node.uuid, 'old': version_before,
                          'new': current_version})
                node.save()
                self._continue_updates(task, update_service, settings)
            return

        # Check if we've been checking for too long
        check_start_time = current_update.get('bmc_check_start_time')

        if check_start_time:
            check_start = timeutils.parse_isotime(check_start_time)
            elapsed_time = timeutils.utcnow(True) - check_start
            timeout = current_update.get(
                'wait', CONF.redfish.firmware_update_reboot_delay)
            if elapsed_time.seconds >= timeout:
                # Timeout: version didn't change or BMC unresponsive
                if (current_version is not None
                        and version_before is not None
                        and current_version == version_before):
                    # Version didn't change - skip reboot
                    LOG.info(
                        'BMC firmware version for node %(node)s did not '
                        'change (still %(version)s). Update appears to be '
                        'a no-op or does not require reboot. Continuing '
                        'without reboot.',
                        {'node': node.uuid, 'version': current_version})
                else:
                    # Version changed or we can't tell - reboot to apply
                    LOG.warning(
                        'BMC firmware version check timeout expired for '
                        'node %(node)s after %(elapsed)s seconds. '
                        'Will reboot to complete firmware update.',
                        {'node': node.uuid, 'elapsed': elapsed_time.seconds})
                    # Mark that reboot is needed
                    node.set_driver_internal_info(
                        FIRMWARE_REBOOT_REQUESTED, True)
                    # Enable reboot flag now that we're ready to reboot
                    deploy_utils.set_async_step_flags(
                        node,
                        reboot=True,
                        polling=True
                    )

                node.del_driver_internal_info(BMC_FW_VERSION_BEFORE_UPDATE)
                node.save()
                self._continue_updates(task, update_service, settings)
                return

        # Continue checking - set wait to check again
        wait_interval = (
            CONF.redfish.firmware_update_bmc_version_check_interval)
        current_update['wait'] = wait_interval
        current_update['wait_start_time'] = str(
            timeutils.utcnow().isoformat())
        current_update['bmc_version_checking'] = True
        node.set_driver_internal_info('redfish_fw_updates', settings)
        node.save()

        LOG.debug('BMC firmware version check continuing for node %(node)s. '
                  'Will check again in %(interval)s seconds.',
                  {'node': node.uuid, 'interval': wait_interval})

    def _submit_simple_update(self, node, update_service, fw_upd):
        """Submit a SimpleUpdate request and track cleanup.

        Handles systems-collection targeting, firmware file staging,
        the SimpleUpdate call, and cleanup tracking.

        :param node: the node that will have a firmware update executed.
        :param update_service: the sushy firmware update service.
        :param fw_upd: single firmware update settings dict (mutated
            in-place: task_monitor and power_timeout are added).
        :returns: task_monitor_uri string
        """
        fw_upd['power_timeout'] = CONF.redfish.firmware_update_reboot_delay

        try:
            systems_collection = redfish_utils.get_system_collection(node)
        except exception.RedfishError as e:
            LOG.error('Failed getting Redfish Systems Collection'
                      ' for node %(node)s. Error %(error)s',
                      {'node': node.uuid, 'error': e})
            raise exception.RedfishError(error=e)
        count = len(systems_collection.members_identities)
        # NOTE(janders) if we see more than one System on the BMC, assume that
        # we need to explicitly specify Target parameter when calling
        # SimpleUpdate. This is needed for compatibility with sushy-tools
        # in automated testing using VMs.
        if count > 1:
            target = node.driver_info.get('redfish_system_id')
            targets = [target]
        else:
            targets = None

        component_url, cleanup = self._stage_firmware_file(node, fw_upd)

        LOG.debug('Applying new firmware %(url)s for %(component)s on node '
                  '%(node_uuid)s',
                  {'url': fw_upd['url'], 'component': fw_upd['component'],
                   'node_uuid': node.uuid})
        try:
            if targets is not None:
                task_monitor = update_service.simple_update(component_url,
                                                            targets=targets)
            else:
                task_monitor = update_service.simple_update(component_url)
        except sushy.exceptions.MissingAttributeError as e:
            LOG.error('The attribute #UpdateService.SimpleUpdate is missing '
                      'on node %(node)s. Error: %(error)s',
                      {'node': node.uuid, 'error': e.message})
            raise exception.RedfishError(error=e)

        fw_upd['task_monitor'] = task_monitor.task_monitor_uri

        if cleanup:
            fw_clean = node.driver_internal_info.get('firmware_cleanup')
            if not fw_clean:
                fw_clean = [cleanup]
            elif cleanup not in fw_clean:
                fw_clean.append(cleanup)
            node.set_driver_internal_info('firmware_cleanup', fw_clean)

        return task_monitor.task_monitor_uri

    def _submit_one_batched_component(self, node, update_service, settings,
                                      idx):
        """Submit a single SimpleUpdate for one component in a batch.

        :param node: the node object
        :param update_service: the sushy firmware update service
        :param settings: list of firmware update dicts
        :param idx: index into settings for the component to submit
        :raises: RedfishError if SimpleUpdate submission fails
        """
        fw_upd = settings[idx]
        component = fw_upd.get('component', '')
        LOG.debug('Batched submission %(idx)d/%(total)d: staging '
                  '%(component)s from %(url)s for node %(node)s',
                  {'idx': idx + 1, 'total': len(settings),
                   'component': component, 'url': fw_upd['url'],
                   'node': node.uuid})
        try:
            self._submit_simple_update(node, update_service, fw_upd)
        except Exception as e:
            LOG.error('Batched firmware submission failed at component '
                      '%(component)s (%(idx)d/%(total)d) for node '
                      '%(node)s. Error: %(error)s. No consolidated '
                      'reboot will be issued.',
                      {'component': component, 'idx': idx + 1,
                       'total': len(settings), 'node': node.uuid,
                       'error': e})
            raise

    def _execute_batched_non_bmc_updates(self, task, update_service, settings):
        """Submit the first non-BMC firmware update and start staging polling.

        Submits SimpleUpdate for the leading run of non-BMC components in
        settings and sets up async polling. The periodic poller will monitor
        staging progress and submit subsequent components one at a time,
        only triggering a consolidated reboot after all are staged.
        BMC entries and any trailing components are left in settings for
        later processing via _start_next_segment.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service
        :param settings: list of firmware update dicts
        :raises: RedfishError if the SimpleUpdate submission fails
        """
        node = task.node
        self._clean_temp_fields(node)

        run_length = self._batch_run_length(node, settings)
        LOG.info('Batching %(batch)d of %(total)d components for node '
                 '%(node)s; remaining components will be processed '
                 'in subsequent segments.',
                 {'batch': run_length, 'total': len(settings),
                  'node': node.uuid})

        self._submit_one_batched_component(node, update_service, settings, 0)

        node.set_driver_internal_info(FIRMWARE_BATCH_CURRENT_INDEX, 0)
        node.set_driver_internal_info('redfish_fw_updates', settings)

        deploy_utils.set_async_step_flags(
            node,
            reboot=False,
            polling=True
        )
        node.save()

        LOG.info('Submitted component 1/%(count)d for node %(node)s. '
                 'Polling for staging completion before submitting next.',
                 {'count': run_length, 'node': node.uuid})

    def _validate_resources_stability(self, node):
        """Validate that BMC resources are consistently available.

        Requires consecutive successful responses from System, Manager,
        and NetworkAdapters resources before considering them stable.
        The number of required successes is configured via
        CONF.redfish.firmware_update_required_successes.
        Timeout is configured via
        CONF.redfish.firmware_update_resource_validation_timeout.

        :param node: the Ironic node object
        :raises: RedfishError if resources don't stabilize within timeout
        """
        timeout = CONF.redfish.firmware_update_resource_validation_timeout
        required_successes = CONF.redfish.firmware_update_required_successes
        validation_interval = CONF.redfish.firmware_update_validation_interval

        # Skip validation if validation is disabled via configuration
        if required_successes == 0 or timeout == 0:
            reasons = []
            if required_successes == 0:
                reasons.append('required_successes=0')
            if timeout == 0:
                reasons.append('validation_timeout=0')

            LOG.info('BMC resource validation disabled (%s) for node %(node)s',
                     ', '.join(reasons), {'node': node.uuid})
            return

        LOG.debug('Starting resource stability validation for node %(node)s '
                  '(timeout: %(timeout)s seconds, '
                  'required_successes: %(required)s, '
                  'validation_interval: %(interval)s seconds)',
                  {'node': node.uuid, 'timeout': timeout,
                   'required': required_successes,
                   'interval': validation_interval})

        start_time = time.time()
        end_time = start_time + timeout
        consecutive_successes = 0
        last_exc = None

        while time.time() < end_time:
            try:
                # Test System resource
                system = redfish_utils.get_system(node)

                # Test Manager resource
                redfish_utils.get_manager(node, system)

                # Test Chassis and NetworkAdapters resource (if available)
                # Some systems may not have NetworkAdapters, which is valid
                chassis = redfish_utils.get_chassis(node, system)
                try:
                    network_adapters = chassis.network_adapters
                    if network_adapters is not None:
                        network_adapters.get_members()
                except sushy.exceptions.MissingAttributeError:
                    # NetworkAdapters not available is acceptable
                    pass

                # All resources successful
                consecutive_successes += 1
                LOG.debug('Resource validation success %(count)d/%(required)d '
                          'for node %(node)s',
                          {'count': consecutive_successes,
                           'required': required_successes,
                           'node': node.uuid})

                if consecutive_successes >= required_successes:
                    LOG.info('All tested Redfish resources stable and '
                             ' available for node %(node)s',
                             {'node': node.uuid})
                    return

            except (exception.RedfishError,
                    exception.RedfishConnectionError,
                    sushy.exceptions.SushyError) as e:
                LOG.debug('BMC resource validation failed for node %(node)s: '
                          '%(error)s. This may indicate the BMC is still '
                          'restarting or recovering from firmware update.',
                          {'node': node.uuid, 'error': e})
                # Resource not available yet, reset counter
                if consecutive_successes > 0:
                    LOG.debug('Resource validation interrupted for node '
                              '%(node)s, resetting success counter '
                              '(error: %(error)s)',
                              {'node': node.uuid, 'error': e})
                consecutive_successes = 0
                last_exc = e

            # Wait before next validation attempt
            time.sleep(validation_interval)
        # Timeout reached without achieving stability
        error_msg = _('BMC resources failed to stabilize within '
                      '%(timeout)s seconds for node %(node)s') % {
            'timeout': timeout, 'node': node.uuid}
        if last_exc:
            error_msg += _(', last error: %(error)s') % {'error': last_exc}
        LOG.error(error_msg)
        raise exception.RedfishError(error=error_msg)

    def _report_step_error(self, task, error_msg, traceback=True):
        """Route a step error to the correct error handler.

        :param task: a TaskManager instance
        :param error_msg: the error message string
        :param traceback: whether to include traceback (default True)
        """
        if task.node.clean_step:
            manager_utils.cleaning_error_handler(
                task, error_msg, traceback=traceback)
        elif task.node.service_step:
            manager_utils.servicing_error_handler(
                task, error_msg, traceback=traceback)
        elif task.node.deploy_step:
            manager_utils.deploying_error_handler(
                task, error_msg, traceback=traceback)
        else:
            LOG.error('No step type set on node %(node)s when attempting '
                      'to report firmware update error: %(error)s',
                      {'node': task.node.uuid, 'error': error_msg})

    def _resume_step(self, task):
        """Notify the conductor to resume the current step.

        :param task: a TaskManager instance
        """
        if task.node.clean_step:
            manager_utils.notify_conductor_resume_clean(task)
        elif task.node.service_step:
            manager_utils.notify_conductor_resume_service(task)
        elif task.node.deploy_step:
            manager_utils.notify_conductor_resume_deploy(task)

    def _continue_updates(self, task, update_service, settings):
        """Continues processing the firmware updates

        Continues to process the firmware updates on the node.
        First monitors the current task completion, then validates resource
        stability before proceeding to next update or completion.

        Note that the caller must have an exclusive lock on the node.

        :param task: a TaskManager instance containing the node to act on.
        :param update_service: the sushy firmware update service
        :param settings: the remaining firmware updates to apply
        """
        node = task.node
        fw_upd = settings[0]

        wait_interval = fw_upd.get('wait')
        if wait_interval:
            time_now = str(timeutils.utcnow().isoformat())
            fw_upd['wait_start_time'] = time_now

            LOG.debug('Waiting at %(time)s for %(seconds)s seconds after '
                      '%(component)s firmware update %(url)s '
                      'on node %(node)s',
                      {'time': time_now,
                       'seconds': wait_interval,
                       'component': fw_upd['component'],
                       'url': fw_upd['url'],
                       'node': node.uuid})

            node.set_driver_internal_info('redfish_fw_updates', settings)
            node.save()
            return

        if len(settings) == 1:
            # Last firmware update - check if reboot is needed
            reboot_requested = node.driver_internal_info.get(
                FIRMWARE_REBOOT_REQUESTED, False)

            self._clear_updates(node)

            LOG.info('Firmware updates completed for node %(node)s',
                     {'node': node.uuid})

            # If reboot was requested (e.g., for BMC timeout),
            # trigger the reboot before notifying conductor
            if reboot_requested:
                LOG.info('Rebooting node %(node)s to apply firmware updates',
                         {'node': node.uuid})
                manager_utils.node_power_action(task, states.REBOOT)

            LOG.debug('Validating BMC responsiveness before resuming '
                      'conductor operations for node %(node)s',
                      {'node': node.uuid})
            self._validate_resources_stability(node)

            try:
                self.cache_firmware_components(task)
            except Exception as e:
                LOG.warning('Failed to refresh firmware components for node '
                            '%(node)s after firmware update: %(error)s',
                            {'node': node.uuid, 'error': e})

            self._resume_step(task)

        else:
            # Validate BMC resources are stable before continuing next update
            LOG.info('Validating BMC responsiveness before continuing '
                     'to next firmware update for node %(node)s',
                     {'node': node.uuid})
            self._validate_resources_stability(node)

            settings.pop(0)
            self._start_next_segment(task, update_service, settings)

    def _start_next_segment(self, task, update_service, settings):
        """Dispatch the next firmware update segment.

        Starts either a batched or sequential firmware update for the
        first component(s) in settings. Called after the previous segment
        has completed and its entry has been removed from settings.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service
        :param settings: remaining firmware update dicts to process
        """
        node = task.node

        run_length = self._batch_run_length(node, settings)
        if run_length > 0:
            self._execute_batched_non_bmc_updates(
                task, update_service, settings)
            node.save()
            return

        # BMC component — sequential path
        fw_upd = settings[0]
        self._submit_simple_update(node, update_service, fw_upd)
        node.set_driver_internal_info('redfish_fw_updates', settings)
        self._setup_bmc_update_monitoring(node, fw_upd)
        node.save()

    def _clear_updates(self, node):
        """Clears firmware updates artifacts

        Clears firmware updates from driver_internal_info and any files
        that were staged.

        Note that the caller must have an exclusive lock on the node.

        :param node: the node to clear the firmware updates from
        """
        firmware_utils.cleanup(node)
        node.del_driver_internal_info('redfish_fw_updates')
        node.del_driver_internal_info('redfish_fw_update_start_time')
        node.del_driver_internal_info('firmware_cleanup')
        node.del_driver_internal_info(FIRMWARE_BATCHED_UPDATE)
        node.del_driver_internal_info(FIRMWARE_ALLOW_GROUPING)
        node.del_driver_internal_info(FIRMWARE_BATCH_SUBMITTED)
        node.del_driver_internal_info(FIRMWARE_BATCH_CURRENT_INDEX)
        node.del_driver_internal_info(FIRMWARE_BATCH_REBOOT_TIME)
        # Clean all temporary fields used during firmware update monitoring
        self._clean_temp_fields(node)
        node.save()

    @METRICS.timer('RedfishFirmware._query_update_failed')
    @periodics.node_periodic(
        purpose='checking if async update of firmware component failed',
        spacing=CONF.redfish.firmware_update_fail_interval,
        filters={'reserved': False, 'provision_state_in': [states.CLEANFAIL,
                 states.DEPLOYFAIL, states.SERVICEFAIL], 'maintenance': True},
        predicate_extra_fields=['driver_internal_info'],
        predicate=lambda n: n.driver_internal_info.get('redfish_fw_updates'),
    )
    def _query_update_failed(self, task, manager, context):

        """Periodic job to check for failed firmware updates."""
        # A firmware update failed. Discard any remaining firmware
        # updates so when the user takes the node out of
        # maintenance mode, pending firmware updates do not
        # automatically continue.
        LOG.error('Update firmware failed for node %(node)s. '
                  'Discarding remaining firmware updates.',
                  {'node': task.node.uuid})

        task.upgrade_lock()
        self._clear_updates(task.node)

    @METRICS.timer('RedfishFirmware._query_update_status')
    @periodics.node_periodic(
        purpose='checking async update of firmware component',
        spacing=CONF.redfish.firmware_update_status_interval,
        filters={'reserved': False, 'provision_state_in': [states.CLEANWAIT,
                 states.DEPLOYWAIT, states.SERVICEWAIT]},
        predicate_extra_fields=['driver_internal_info'],
        predicate=lambda n: n.driver_internal_info.get('redfish_fw_updates'),
    )
    def _query_update_status(self, task, manager, context):
        """Periodic job to check firmware update tasks."""
        self._check_node_redfish_firmware_update(task)

    def _handle_task_completion(self, task, sushy_task, messages,
                                update_service, settings, current_update):
        """Handle firmware update task completion.

        :param task: a TaskManager instance
        :param sushy_task: the sushy task object
        :param messages: list of task messages
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        :param current_update: the current firmware update being processed
        """
        node = task.node

        if (sushy_task.task_state == sushy.TASK_STATE_COMPLETED
                and sushy_task.task_status in
                [sushy.HEALTH_OK, sushy.HEALTH_WARNING]):
            LOG.debug('Redfish task completed for node %(node)s, '
                      'firmware %(firmware_image)s: %(messages)s.',
                      {'node': node.uuid,
                       'firmware_image': current_update['url'],
                       'messages': ", ".join(messages)})

            component = current_update.get('component', '')
            component_type = redfish_utils.get_component_type(component)

            if component_type == redfish_utils.BMC:
                self._handle_bmc_update_completion(
                    task, update_service, settings, current_update)
            else:
                self._continue_updates(task, update_service, settings)
        else:
            error_msg = (_('Firmware update failed for node %(node)s, '
                           'firmware %(firmware_image)s. '
                           'Error: %(errors)s') %
                         {'node': node.uuid,
                          'firmware_image': current_update['url'],
                          'errors': ",  ".join(messages)})

            self._clear_updates(node)
            self._report_step_error(task, error_msg)

    def _handle_wait_completion(self, task, update_service, settings,
                                current_update):
        """Handle firmware update wait completion.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        :param current_update: the current firmware update being processed
        """
        # Upgrade lock at the start since we may modify driver_internal_info
        task.upgrade_lock()
        node = task.node

        # Check if this is BMC version checking
        if current_update.get('bmc_version_checking'):
            current_update.pop('bmc_version_checking', None)
            node.set_driver_internal_info(
                'redfish_fw_updates', settings)
            node.save()
            # Continue BMC version checking
            self._handle_bmc_update_completion(
                task, update_service, settings, current_update)
        elif current_update.get('component_type') == redfish_utils.BMC:
            # BMC update wait expired - check if task is still running
            # before transitioning to version checking
            task_still_running = False
            try:
                task_monitor = redfish_utils.get_task_monitor(
                    node, current_update['task_monitor'])
                if task_monitor.is_processing:
                    task_still_running = True
                    LOG.debug('BMC firmware update wait expired but task '
                              ' still processing for node %(node)s. '
                              'Continuing to monitor task completion.',
                              {'node': node.uuid})
            except exception.RedfishConnectionError as e:
                LOG.debug('Unable to communicate with task monitor for node '
                          '%(node)s during wait completion: %(error)s. '
                          'BMC may be resetting, will transition to version '
                          'checking.', {'node': node.uuid, 'error': e})
            except exception.RedfishError as e:
                LOG.debug('Task monitor unavailable for node %(node)s: '
                          '%(error)s. Task may have completed, transitioning '
                          'to version checking.',
                          {'node': node.uuid, 'error': e})

            if task_still_running:
                # Task is still running, continue to monitor task completion
                # Don't transition to version checking yet.
                node.set_driver_internal_info('redfish_fw_updates', settings)
                node.save()
                return

            # Task completed, deleted or BMC unavailable
            # Transition to version checking
            LOG.info('BMC firmware update wait expired for node %(node)s. '
                     'Task completed or unavailable. Transitioning to version '
                     'checking mode.',
                     {'node': node.uuid})
            self._handle_bmc_update_completion(
                task, update_service, settings, current_update)

    def _check_overall_timeout(self, task):
        """Check if firmware update has exceeded overall timeout.

        :param task: A TaskManager instance
        :returns: True if timeout exceeded and error was handled,
                  False otherwise
        """
        node = task.node
        overall_timeout = CONF.redfish.firmware_update_overall_timeout
        if overall_timeout <= 0:
            return False

        start_time_str = node.driver_internal_info.get(
            'redfish_fw_update_start_time')
        if not start_time_str:
            return False

        start_time = timeutils.parse_isotime(start_time_str)
        elapsed = timeutils.utcnow(True) - start_time
        if elapsed.total_seconds() < overall_timeout:
            return False

        msg = (_('Firmware update on node %(node)s has exceeded '
                 'the overall timeout of %(timeout)s seconds. '
                 'Elapsed time: %(elapsed)s seconds.')
               % {'node': node.uuid,
                  'timeout': overall_timeout,
                  'elapsed': int(elapsed.total_seconds())})
        LOG.error(msg)
        task.upgrade_lock()
        settings = node.driver_internal_info.get('redfish_fw_updates', [])
        msg += self._staged_pending_note(node, settings)
        self._clear_updates(node)
        self._report_step_error(task, msg, traceback=False)
        return True

    def _handle_firmware_update_task(self, task, node, current_update,
                                     update_service, settings):
        """Handle the firmware update task monitoring and completion.

        :param task: a TaskManager instance
        :param node: an Ironic node object
        :param current_update: the current firmware update being processed
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        """
        try:
            task_monitor = redfish_utils.get_task_monitor(
                node, current_update['task_monitor'])
        except exception.RedfishConnectionError as e:
            # If the BMC firmware is being updated, the BMC will be
            # unavailable for some amount of time.
            LOG.warning('Unable to communicate with task monitor service '
                        'on node %(node)s. Will try again on the next poll. '
                        'Error: %(error)s',
                        {'node': node.uuid, 'error': e})
            return
        except exception.RedfishError:
            LOG.warning('Firmware update completed for node %(node)s, '
                        'firmware %(firmware_image)s, but success of the '
                        'update is unknown.  Assuming update was successful.',
                        {'node': node.uuid,
                         'firmware_image': current_update['url']})
            self._continue_updates(task, update_service, settings)
            return

        try:
            # The last response does not necessarily contain a Task,
            # so get it
            sushy_task = task_monitor.get_task()
            task_state = sushy_task.task_state
        except Exception as e:
            LOG.warning('Unable to get task for node %(node)s: %(error)s. '
                        'Will retry on next poll.',
                        {'node': node.uuid, 'error': e})
            return

        # Check if task is in a terminal state (completed, failed, etc.)
        # If so, proceed directly to completion handling
        if task_state not in [sushy.TASK_STATE_NEW,
                              sushy.TASK_STATE_RUNNING,
                              sushy.TASK_STATE_STARTING,
                              sushy.TASK_STATE_PENDING]:
            # Task is done (COMPLETED, EXCEPTION, KILLED, CANCELLED, etc.)
            # Parse messages and handle completion
            LOG.debug('Firmware update task in terminal state %(state)s '
                      'for node %(node)s',
                      {'state': task_state, 'node': node.uuid})

            # Only parse the messages if the BMC did not return parsed
            # messages
            messages = []
            if sushy_task.messages and not sushy_task.messages[0].message:
                sushy_task.parse_messages()

            if sushy_task.messages is not None:
                for m in sushy_task.messages:
                    msg = m.message
                    if not msg or msg.lower() in ['unknown', 'unknown error']:
                        msg = m.message_id
                    if msg:
                        messages.append(msg)

            self._handle_task_completion(task, sushy_task, messages,
                                         update_service, settings,
                                         current_update)
            return

        LOG.debug('Firmware update in progress for node %(node)s, '
                  'firmware %(firmware_image)s.',
                  {'node': node.uuid,
                   'firmware_image': current_update['url']})

    @METRICS.timer('RedfishFirmware._check_node_redfish_firmware_update')
    def _check_node_redfish_firmware_update(self, task):
        """Check the progress of running firmware update on a node."""
        # Upgrade the lock to ensure we are using the latest info from
        # the node.
        task.upgrade_lock()
        node = task.node

        # Check overall timeout for firmware update operation
        if self._check_overall_timeout(task):
            return

        settings = node.driver_internal_info['redfish_fw_updates']
        current_update = settings[0]

        try:
            update_service = redfish_utils.get_update_service(node)
        except exception.RedfishConnectionError as e:
            # If the BMC firmware is being updated, the BMC will be
            # unavailable for some amount of time.
            LOG.warning('Unable to communicate with firmware update service '
                        'on node %(node)s. Will try again on the next poll. '
                        'Error: %(error)s',
                        {'node': node.uuid, 'error': e})
            return

        # Check if BMC update just completed and node rebooted
        # If so, continue with next component update
        if current_update.get(BMC_UPDATE_COMPLETED):
            LOG.info('BMC firmware update completed and node %(node)s has '
                     'rebooted. Continuing with next component.',
                     {'node': node.uuid})
            current_update.pop(BMC_UPDATE_COMPLETED, None)
            node.set_driver_internal_info('redfish_fw_updates', settings)
            node.save()

            self._continue_updates(task, update_service, settings)
            return

        # Touch provisioning to indicate progress is being monitored.
        # This prevents heartbeat timeout from triggering for steps that
        # don't require the ramdisk agent (requires_ramdisk=False).
        # Note: Only touch after successful BMC communication to ensure
        # the process eventually times out if the BMC is unresponsive.
        node.touch_provisioning()

        if (node.driver_internal_info.get(FIRMWARE_BATCHED_UPDATE)
                and (node.driver_internal_info.get(FIRMWARE_BATCH_SUBMITTED)
                     or node.driver_internal_info.get(
                         FIRMWARE_BATCH_CURRENT_INDEX) is not None)):
            self._check_batched_update_status(task, settings)
            return

        wait_start_time = current_update.get('wait_start_time')
        if wait_start_time:
            wait_start = timeutils.parse_isotime(wait_start_time)

            elapsed_time = timeutils.utcnow(True) - wait_start
            if elapsed_time.seconds >= current_update['wait']:
                LOG.debug('Finished waiting after firmware update '
                          '%(firmware_image)s on node %(node)s. '
                          'Elapsed time: %(seconds)s seconds',
                          {'firmware_image': current_update['url'],
                           'node': node.uuid,
                           'seconds': elapsed_time.seconds})
                current_update.pop('wait', None)
                current_update.pop('wait_start_time', None)

                # Handle wait completion
                self._handle_wait_completion(
                    task, update_service, settings, current_update)
            else:
                LOG.debug('Continuing to wait after firmware update '
                          '%(firmware_image)s on node %(node)s. '
                          'Elapsed time: %(seconds)s seconds',
                          {'firmware_image': current_update['url'],
                           'node': node.uuid,
                           'seconds': elapsed_time.seconds})

            return

        # Handle firmware update task monitoring
        self._handle_firmware_update_task(
            task, node, current_update, update_service, settings)

    def _check_batched_update_status(self, task, settings):
        """Check batched firmware update status (two-phase).

        Phase 1 (staging): polls the current component's task. When staged,
        submits the next component or transitions to Phase 2.
        Phase 2 (post-reboot): polls ALL task monitors for completion.

        :param task: a TaskManager instance
        :param settings: firmware update settings with task_monitor URIs
        """
        node = task.node
        if node.driver_internal_info.get(FIRMWARE_BATCH_SUBMITTED):
            self._check_batched_post_reboot(task, settings)
        else:
            self._check_batched_staging(task, settings)

    def _check_batched_staging(self, task, settings):
        """Phase 1: poll the current component and advance when staged."""
        node = task.node
        current_idx = node.driver_internal_info.get(
            FIRMWARE_BATCH_CURRENT_INDEX, 0)
        fw_upd = settings[current_idx]
        component = fw_upd.get('component', '')
        monitor_uri = fw_upd.get('task_monitor')

        if not monitor_uri:
            LOG.debug('No task monitor for %(component)s on node %(node)s. '
                      'Treating as staged.',
                      {'component': component, 'node': node.uuid})
            self._advance_batch_staging(task, settings, current_idx)
            return

        try:
            task_monitor = redfish_utils.get_task_monitor(node, monitor_uri)
        except exception.RedfishConnectionError as e:
            LOG.warning('Unable to reach task monitor for %(component)s '
                        'on node %(node)s: %(error)s. Will retry.',
                        {'component': component,
                         'node': node.uuid, 'error': e})
            return
        except exception.RedfishError:
            LOG.debug('Task monitor for %(component)s disappeared on '
                      'node %(node)s. Treating as staged.',
                      {'component': component, 'node': node.uuid})
            self._advance_batch_staging(task, settings, current_idx)
            return

        try:
            sushy_task = task_monitor.get_task()
        except Exception as e:
            LOG.warning('Unable to get task for %(component)s on node '
                        '%(node)s: %(error)s. Will retry.',
                        {'component': component,
                         'node': node.uuid, 'error': e})
            return

        if sushy_task.task_state in [sushy.TASK_STATE_NEW,
                                     sushy.TASK_STATE_PENDING,
                                     sushy.TASK_STATE_RUNNING]:
            LOG.debug('Component %(component)s still staging on node '
                      '%(node)s (state=%(state)s). Will retry.',
                      {'component': component, 'node': node.uuid,
                       'state': sushy_task.task_state})
            return

        # Starting = "staged, scheduled for apply at reboot" (Dell BIOS/SSD
        # pattern). In Phase 2 post-reboot, Starting means "still running".
        if sushy_task.task_state in [sushy.TASK_STATE_STARTING,
                                     sushy.TASK_STATE_COMPLETED]:
            if (sushy_task.task_state == sushy.TASK_STATE_COMPLETED
                    and sushy_task.task_status not in
                    [sushy.HEALTH_OK, sushy.HEALTH_WARNING]):
                self._fail_batched_update(
                    task, node, fw_upd, sushy_task, settings)
                return
            LOG.info('Component %(component)s staged on node %(node)s '
                     '(state=%(state)s).',
                     {'component': component, 'node': node.uuid,
                      'state': sushy_task.task_state})
            self._advance_batch_staging(task, settings, current_idx)
            return

        self._fail_batched_update(task, node, fw_upd, sushy_task, settings)

    def _advance_batch_staging(self, task, settings, current_idx):
        """Submit next component or trigger reboot if all staged."""
        node = task.node
        next_idx = current_idx + 1
        run_length = self._batch_run_length(node, settings)

        if next_idx < run_length:
            try:
                update_service = redfish_utils.get_update_service(node)
            except exception.RedfishError as e:
                error_msg = (
                    _('Failed to get update service for node %(node)s '
                      'while advancing batch: %(error)s')
                    % {'node': node.uuid, 'error': e})
                LOG.error(error_msg)
                error_msg += self._staged_pending_note(
                    node, settings)
                self._clear_updates(node)
                self._report_step_error(task, error_msg)
                return

            try:
                self._submit_one_batched_component(
                    node, update_service, settings, next_idx)
            except Exception as e:
                error_msg = (
                    _('Batched firmware submission failed at component '
                      '%(component)s (%(idx)d/%(total)d) for node '
                      '%(node)s. Error: %(error)s')
                    % {'component': settings[next_idx].get('component', ''),
                       'idx': next_idx + 1, 'total': run_length,
                       'node': node.uuid, 'error': e})
                LOG.error(error_msg)
                error_msg += self._staged_pending_note(
                    node, settings)
                self._clear_updates(node)
                self._report_step_error(task, error_msg)
                return

            node.set_driver_internal_info(FIRMWARE_BATCH_CURRENT_INDEX,
                                          next_idx)
            node.set_driver_internal_info('redfish_fw_updates', settings)
            node.save()
            LOG.info('Submitted component %(idx)d/%(total)d for node '
                     '%(node)s. Polling for staging completion.',
                     {'idx': next_idx + 1, 'total': run_length,
                      'node': node.uuid})
        else:
            node.set_driver_internal_info(
                FIRMWARE_BATCH_REBOOT_TIME,
                timeutils.utcnow().isoformat())
            node.del_driver_internal_info(FIRMWARE_BATCH_CURRENT_INDEX)
            node.set_driver_internal_info(FIRMWARE_BATCH_SUBMITTED, True)
            node.set_driver_internal_info('redfish_fw_updates', settings)
            node.save()

            LOG.info('All %(count)d batch components staged for node '
                     '%(node)s. Triggering consolidated reboot.',
                     {'count': run_length, 'node': node.uuid})
            deploy_utils.set_async_step_flags(
                node, reboot=True, polling=True)
            power_timeout = settings[0].get('power_timeout', 0)
            manager_utils.node_power_action(task, states.REBOOT,
                                            power_timeout)

    def _check_batched_post_reboot(self, task, settings):
        """Phase 2: poll all task monitors for final completion."""
        node = task.node

        reboot_time = node.driver_internal_info.get(FIRMWARE_BATCH_REBOOT_TIME)
        if reboot_time:
            elapsed = (timeutils.utcnow(True)
                       - timeutils.parse_isotime(reboot_time))
            min_wait = CONF.redfish.firmware_update_status_interval
            if elapsed.total_seconds() < min_wait:
                LOG.debug('Too early to poll after reboot for node %(node)s '
                          '(%(elapsed)ds < %(min)ds). Will retry.',
                          {'node': node.uuid,
                           'elapsed': int(elapsed.total_seconds()),
                           'min': min_wait})
                return
            try:
                self._validate_resources_stability(node)
            except exception.RedfishError:
                LOG.debug('BMC not yet stable after reboot for node %s, '
                          'will retry', node.uuid)
                return
            node.del_driver_internal_info(FIRMWARE_BATCH_REBOOT_TIME)
            node.save()

        run_length = self._batch_run_length(node, settings)
        completed = 0
        still_running = 0

        for fw_upd in settings[:run_length]:
            monitor_uri = fw_upd.get('task_monitor')
            if not monitor_uri:
                completed += 1
                continue

            try:
                task_monitor = redfish_utils.get_task_monitor(
                    node, monitor_uri)
            except exception.RedfishConnectionError as e:
                LOG.warning('Unable to reach task monitor for %(component)s '
                            'on node %(node)s: %(error)s. Will retry.',
                            {'component': fw_upd.get('component', ''),
                             'node': node.uuid, 'error': e})
                still_running += 1
                continue
            except exception.RedfishError:
                LOG.debug('Task monitor for %(component)s disappeared on '
                          'node %(node)s. Assuming completed.',
                          {'component': fw_upd.get('component', ''),
                           'node': node.uuid})
                fw_upd.pop('task_monitor', None)
                completed += 1
                continue

            try:
                sushy_task = task_monitor.get_task()
            except Exception as e:
                LOG.warning('Unable to get task for %(component)s on node '
                            '%(node)s: %(error)s. Will retry.',
                            {'component': fw_upd.get('component', ''),
                             'node': node.uuid, 'error': e})
                still_running += 1
                continue

            # Starting = "still applying" post-reboot (will transition to
            # Completed during POST). In Phase 1 staging, Starting means
            # "staged".
            if sushy_task.task_state in [sushy.TASK_STATE_NEW,
                                         sushy.TASK_STATE_RUNNING,
                                         sushy.TASK_STATE_STARTING,
                                         sushy.TASK_STATE_PENDING]:
                still_running += 1
                continue

            if (sushy_task.task_state == sushy.TASK_STATE_COMPLETED
                    and sushy_task.task_status in
                    [sushy.HEALTH_OK, sushy.HEALTH_WARNING]):
                fw_upd.pop('task_monitor', None)
                completed += 1
                continue

            self._fail_batched_update(
                task, node, fw_upd, sushy_task, settings)
            return

        LOG.debug('Batched firmware update progress for node %(node)s: '
                  '%(completed)d/%(total)d completed, '
                  '%(running)d still running',
                  {'node': node.uuid, 'completed': completed,
                   'total': run_length, 'running': still_running})

        node.set_driver_internal_info('redfish_fw_updates', settings)
        node.save()

        if still_running == 0:
            self._finalize_batched_update(task)

    def _fail_batched_update(self, task, node, fw_upd, sushy_task,
                             settings):
        """Handle a failed task during batched firmware update."""
        messages = []
        if sushy_task.messages:
            if not sushy_task.messages[0].message:
                sushy_task.parse_messages()
            for m in sushy_task.messages:
                msg = m.message
                if not msg or msg.lower() in ['unknown', 'unknown error']:
                    msg = m.message_id
                if msg:
                    messages.append(msg)

        error_msg = (
            _('Batched firmware update failed for component '
              '%(component)s on node %(node)s. Error: %(errors)s')
            % {'component': fw_upd.get('component', ''),
               'node': node.uuid,
               'errors': ', '.join(messages)})
        LOG.error(error_msg)
        error_msg += self._staged_pending_note(
            node, settings, exclude=fw_upd)
        self._clear_updates(node)
        self._report_step_error(task, error_msg)

    def _finalize_batched_update(self, task):
        """Complete the current batch segment and hand off if more remain.

        Pops the completed batch components from settings. If more
        components remain, hands off to _start_next_segment for the next
        segment (which might be a sequential BMC update or another batch).
        Otherwise, validates stability, caches firmware, and resumes.

        :param task: a TaskManager instance
        """
        node = task.node
        settings = node.driver_internal_info.get('redfish_fw_updates', [])
        run_length = self._batch_run_length(node, settings)

        LOG.info('Batch segment of %(count)d components completed for node '
                 '%(node)s.',
                 {'count': run_length, 'node': node.uuid})

        del settings[:run_length]

        node.del_driver_internal_info(FIRMWARE_BATCH_SUBMITTED)
        node.del_driver_internal_info(FIRMWARE_BATCH_REBOOT_TIME)
        node.del_driver_internal_info(FIRMWARE_BATCH_CURRENT_INDEX)

        if settings:
            LOG.info('%(remaining)d components remaining for node %(node)s. '
                     'Continuing with next segment.',
                     {'remaining': len(settings), 'node': node.uuid})
            node.set_driver_internal_info('redfish_fw_updates', settings)
            node.save()

            try:
                update_service = redfish_utils.get_update_service(node)
            except exception.RedfishError as e:
                error_msg = (
                    _('Failed to get update service for node %(node)s '
                      'while continuing after batch: %(error)s')
                    % {'node': node.uuid, 'error': e})
                LOG.error(error_msg)
                self._clear_updates(node)
                self._report_step_error(task, error_msg)
                return

            self._start_next_segment(task, update_service, settings)
            return

        LOG.debug('Validating BMC responsiveness before resuming '
                  'conductor operations for node %(node)s',
                  {'node': node.uuid})
        try:
            self._validate_resources_stability(node)
        except exception.RedfishError:
            LOG.warning('BMC resources did not stabilize for node %(node)s '
                        'after batched firmware update, but proceeding '
                        'with finalization.',
                        {'node': node.uuid})

        try:
            self.cache_firmware_components(task)
        except Exception as e:
            LOG.warning('Failed to refresh firmware components for node '
                        '%(node)s after batched update: %(error)s',
                        {'node': node.uuid, 'error': e})

        self._clear_updates(node)
        self._resume_step(task)

    def _stage_firmware_file(self, node, component_update):

        try:
            url = component_update['url']
            name = component_update['component']
            parsed_url = urlparse(url)
            scheme = parsed_url.scheme.lower()
            source = (CONF.redfish.firmware_source).lower()

            # Keep it simple, in further processing TLS does not matter
            if scheme == 'https':
                scheme = 'http'

            # If source and scheme is HTTP, then no staging,
            # returning original location
            if scheme == 'http' and source == scheme:
                LOG.debug('For node %(node)s serving firmware for '
                          '%(component)s from original location %(url)s',
                          {'node': node.uuid, 'component': name, 'url': url})
                return url, None

            # If source and scheme is Swift, then not moving, but
            # returning Swift temp URL
            if scheme == 'swift' and source == scheme:
                temp_url = firmware_utils.get_swift_temp_url(parsed_url)
                LOG.debug('For node %(node)s serving original firmware at '
                          'for %(component)s at %(url)s via Swift temporary '
                          'url %(temp_url)s',
                          {'node': node.uuid, 'component': name, 'url': url,
                           'temp_url': temp_url})
                return temp_url, None

            # For remaining, download the image to temporary location
            temp_file = firmware_utils.download_to_temp(node, url)

            return firmware_utils.stage(node, source, temp_file)

        except exception.IronicException:
            firmware_utils.cleanup(node)
            raise
