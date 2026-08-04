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

import datetime
import json
import time
from unittest import mock

from oslo_config import cfg
from oslo_utils import timeutils
import sushy

from ironic.common import async_steps
from ironic.common import exception
from ironic.common import states
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.conf import CONF
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules.redfish import firmware as redfish_fw
from ironic.drivers.modules.redfish import firmware_utils
from ironic.drivers.modules.redfish import utils as redfish_utils
from ironic import objects
from ironic.tests.unit.db import base as db_base
from ironic.tests.unit.db import utils as db_utils
from ironic.tests.unit.objects import utils as obj_utils

INFO_DICT = db_utils.get_test_redfish_info()


class RedfishFirmwareTestCase(db_base.DbTestCase):

    def setUp(self):
        super(RedfishFirmwareTestCase, self).setUp()
        self.config(enabled_bios_interfaces=['redfish'],
                    enabled_hardware_types=['redfish'],
                    enabled_power_interfaces=['redfish'],
                    enabled_boot_interfaces=['redfish-virtual-media'],
                    enabled_management_interfaces=['redfish'],
                    enabled_firmware_interfaces=['redfish'])
        self.node = obj_utils.create_test_node(
            self.context, driver='redfish', driver_info=INFO_DICT)

    def test_get_properties(self):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            properties = task.driver.get_properties()
            for prop in redfish_utils.COMMON_PROPERTIES:
                self.assertIn(prop, properties)

    @mock.patch.object(redfish_utils, 'parse_driver_info', autospec=True)
    def test_validate(self, mock_parse_driver_info):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.firmware.validate(task)
            mock_parse_driver_info.assert_called_once_with(task.node)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects.FirmwareComponentList,
                       'sync_firmware_components', autospec=True)
    def test_missing_all_components(self, sync_fw_cmp_mock, chassis_mock,
                                    manager_mock, system_mock, log_mock):

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            system_mock.return_value.identity = "System1"
            manager_mock.return_value.identity = "Manager1"
            system_mock.return_value.bios_version = None
            manager_mock.return_value.firmware_version = None

            netadp = mock.MagicMock()
            netadp.get_members.return_value = []
            chassis_mock.return_value.network_adapters = netadp

            self.assertRaises(exception.UnsupportedDriverExtension,
                              task.driver.firmware.cache_firmware_components,
                              task)

            sync_fw_cmp_mock.assert_not_called()
            error_msg = (
                'Cannot retrieve firmware for node %s: '
                'no supported components'
                % self.node.uuid)
            log_mock.error.assert_called_once_with(error_msg)

            debug_calls = [
                mock.call('Could not retrieve BiosVersion in node '
                          '%(node_uuid)s system %(system)s',
                          {'node_uuid': self.node.uuid,
                           'system': "System1"}),
                mock.call('Could not retrieve FirmwareVersion in node '
                          '%(node_uuid)s manager %(manager)s',
                          {'node_uuid': self.node.uuid,
                           'manager': "Manager1"}),
                mock.call('Could not retrieve Firmware Package Version '
                          'from NetworkAdapters on node %(node_uuid)s',
                          {'node_uuid': self.node.uuid})]
            log_mock.debug.assert_has_calls(debug_calls)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects.FirmwareComponentList,
                       'sync_firmware_components', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_missing_bios_component(self, fw_cmp_mock, sync_fw_cmp_mock,
                                    manager_mock, system_mock, log_mock):
        create_list = [{'component': 'bmc', 'current_version': 'v1.0.0'}]
        sync_fw_cmp_mock.return_value = (
            create_list, [], []
        )

        bmc_component = {'component': 'bmc', 'current_version': 'v1.0.0',
                         'node_id': self.node.id}

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            system_mock.return_value.identity = "System1"
            system_mock.return_value.bios_version = None
            manager_mock.return_value.firmware_version = "v1.0.0"

            task.driver.firmware.cache_firmware_components(task)
            system_mock.assert_called_once_with(task.node)

            log_mock.debug.assert_any_call(
                'Could not retrieve BiosVersion in node '
                '%(node_uuid)s system %(system)s',
                {'node_uuid': self.node.uuid, 'system': 'System1'})
            sync_fw_cmp_mock.assert_called_once_with(
                task.context, task.node.id,
                [{'component': 'bmc', 'current_version': 'v1.0.0'}])
            self.assertTrue(fw_cmp_mock.called)
            fw_cmp_mock.assert_called_once_with(task.context, **bmc_component)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects.FirmwareComponentList,
                       'sync_firmware_components', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_missing_bmc_component(self, fw_cmp_mock, sync_fw_cmp_mock,
                                   manager_mock, system_mock, log_mock):
        create_list = [{'component': 'bios', 'current_version': 'v1.0.0'}]
        sync_fw_cmp_mock.return_value = (
            create_list, [], []
        )

        bios_component = {'component': 'bios', 'current_version': 'v1.0.0',
                          'node_id': self.node.id}

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            manager_mock.return_value.identity = "Manager1"
            manager_mock.return_value.firmware_version = None
            system_mock.return_value.bios_version = "v1.0.0"
            task.driver.firmware.cache_firmware_components(task)

            log_mock.debug.assert_any_call(
                'Could not retrieve FirmwareVersion in node '
                '%(node_uuid)s manager %(manager)s',
                {'node_uuid': self.node.uuid, 'manager': "Manager1"})
            system_mock.assert_called_once_with(task.node)
            sync_fw_cmp_mock.assert_called_once_with(
                task.context, task.node.id,
                [{'component': 'bios', 'current_version': 'v1.0.0'}])
            self.assertTrue(fw_cmp_mock.called)
            fw_cmp_mock.assert_called_once_with(task.context, **bios_component)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_create_all_components(self, fw_cmp_mock, fw_cmp_list_mock,
                                   chassis_mock, manager_mock, system_mock,
                                   log_mock):
        create_list = [{'component': 'bios', 'current_version': 'v1.0.0'},
                       {'component': 'bmc', 'current_version': 'v1.0.0'},
                       {'component': 'nic:NIC1', 'current_version': '1'}]
        fw_cmp_list_mock.sync_firmware_components.return_value = (
            create_list, [], []
        )

        bios_component = {'component': 'bios', 'current_version': 'v1.0.0',
                          'node_id': self.node.id}

        bmc_component = {'component': 'bmc', 'current_version': 'v1.0.0',
                         'node_id': self.node.id}

        nic_component = {'component': 'nic:NIC1', 'current_version': '1',
                         'node_id': self.node.id}

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            manager_mock.return_value.firmware_version = "v1.0.0"
            system_mock.return_value.bios_version = "v1.0.0"
            chassis_mock
            netadp_ctrl = mock.MagicMock()
            netadp_ctrl.firmware_package_version = "1"
            netadp = mock.MagicMock()
            netadp.identity = 'NIC1'
            netadp.serial_number = None
            netadp.controllers = [netadp_ctrl]
            net_adapters = mock.MagicMock()
            net_adapters.get_members.return_value = [netadp]
            chassis_mock.return_value.network_adapters = net_adapters
            task.driver.firmware.cache_firmware_components(task)

            log_mock.warning.assert_not_called()
            log_mock.debug.assert_called_once_with(
                'Using Identity %(identity)s for '
                'NetworkAdapter %(net_adp_id)s',
                {'identity': 'NIC1', 'net_adp_id': 'NIC1'})
            system_mock.assert_called_once_with(task.node)
            fw_cmp_list_mock.sync_firmware_components.assert_called_once_with(
                task.context, task.node.id,
                [{'component': 'bios', 'current_version': 'v1.0.0'},
                 {'component': 'bmc', 'current_version': 'v1.0.0'},
                 {'component': 'nic:NIC1', 'current_version': '1'}])
            fw_cmp_calls = [
                mock.call(task.context, **bios_component),
                mock.call().create(),
                mock.call(task.context, **bmc_component),
                mock.call().create(),
                mock.call(task.context, **nic_component),
                mock.call().create()
            ]
            fw_cmp_mock.assert_has_calls(fw_cmp_calls)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_get_chassis_redfish_error(self, sync_fw_cmp_mock, system_mock,
                                       manager_mock, chassis_mock, log_mock):
        system_mock.return_value.identity = "System1"
        system_mock.return_value.bios_version = '1.0.0'
        manager_mock.return_value.identity = "Manager1"
        manager_mock.return_value.firmware_version = '1.0.0'

        chassis_mock.side_effect = exception.RedfishError('not found')

        sync_fw_cmp_mock.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'},],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.firmware.cache_firmware_components(task)

        log_mock.debug.assert_any_call(
            'No chassis available to retrieve NetworkAdapters firmware '
            'information on node %(node_uuid)s',
            {'node_uuid': self.node.uuid}
        )

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_redfish_connection_error(
            self, sync_fw_cmp_mock, manager_mock, system_mock, log_mock):
        """Test that RedfishConnectionError during NIC retrieval is handled."""
        system_mock.return_value.identity = "System1"
        system_mock.return_value.bios_version = '1.0.0'
        manager_mock.return_value.identity = "Manager1"
        manager_mock.return_value.firmware_version = '1.0.0'

        sync_fw_cmp_mock.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(task.driver.firmware,
                                   'retrieve_nic_components',
                                   autospec=True) as mock_retrieve:
                connection_error = exception.RedfishError(
                    'Connection failed')
                mock_retrieve.side_effect = connection_error

                task.driver.firmware.cache_firmware_components(task)

        # Verify warning log for exception is called
        log_mock.warning.assert_any_call(
            'Unable to access NetworkAdapters on node %(node_uuid)s, '
            'Error: %(error)s',
            {'node_uuid': self.node.uuid, 'error': connection_error}
        )

        # Verify debug log for empty NIC list is NOT called
        # (since we caught an exception, not an empty list)
        debug_calls = [call for call in log_mock.debug.call_args_list
                       if 'Could not retrieve Firmware Package Version from '
                          'NetworkAdapters' in str(call)]
        self.assertEqual(len(debug_calls), 0)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_sushy_bad_request_error(
            self, sync_fw_cmp_mock, manager_mock, system_mock, log_mock):
        """Test that sushy BadRequestError during NIC retrieval is handled."""
        system_mock.return_value.identity = "System1"
        system_mock.return_value.bios_version = '1.0.0'
        manager_mock.return_value.identity = "Manager1"
        manager_mock.return_value.firmware_version = '1.0.0'

        sync_fw_cmp_mock.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(task.driver.firmware,
                                   'retrieve_nic_components',
                                   autospec=True) as mock_retrieve:
                bad_request_error = sushy.exceptions.BadRequestError(
                    method='GET', url='/redfish/v1/Chassis/1/NetworkAdapters',
                    response=mock.Mock(status_code=400))
                mock_retrieve.side_effect = bad_request_error

                task.driver.firmware.cache_firmware_components(task)

        # Verify warning log for exception is called
        log_mock.warning.assert_any_call(
            'Unable to access NetworkAdapters on node %(node_uuid)s, '
            'Error: %(error)s',
            {'node_uuid': self.node.uuid, 'error': bad_request_error}
        )

        # Verify debug log for empty NIC list is NOT called
        # (since we caught an exception, not an empty list)
        debug_calls = [call for call in log_mock.debug.call_args_list
                       if 'Could not retrieve Firmware Package Version from '
                          'NetworkAdapters' in str(call)]
        self.assertEqual(len(debug_calls), 0)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_sushy_missing_attribute_error(
            self, sync_fw_cmp_mock, manager_mock, system_mock, log_mock):
        """Test that MissingAttributeError during NIC retrieval is handled."""
        system_mock.return_value.identity = "System1"
        system_mock.return_value.bios_version = '1.0.0'
        manager_mock.return_value.identity = "Manager1"
        manager_mock.return_value.firmware_version = '1.0.0'

        sync_fw_cmp_mock.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(task.driver.firmware,
                                   'retrieve_nic_components',
                                   autospec=True) as mock_retrieve:
                missing_attr_error = sushy.exceptions.MissingAttributeError(
                    attribute='NetworkAdapters',
                    resource='/redfish/v1/Chassis/1')
                mock_retrieve.side_effect = missing_attr_error

                task.driver.firmware.cache_firmware_components(task)

        # Verify warning log for exception is called
        log_mock.warning.assert_any_call(
            'Unable to access NetworkAdapters on node %(node_uuid)s, '
            'Error: %(error)s',
            {'node_uuid': self.node.uuid, 'error': missing_attr_error}
        )

        # Verify debug log for empty NIC list is NOT called
        # (since we caught an exception, not an empty list)
        debug_calls = [call for call in log_mock.debug.call_args_list
                       if 'Could not retrieve Firmware Package Version from '
                          'NetworkAdapters' in str(call)]
        self.assertEqual(len(debug_calls), 0)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_retrieve_nic_components_invalid_firmware_version(
            self, fw_cmp_mock, fw_cmp_list, chassis_mock, manager_mock,
            system_mock, log_mock):
        """Test that NIC components with missing versions are skipped."""
        for invalid_version in [None, ""]:
            fw_cmp_list.reset_mock()
            fw_cmp_mock.reset_mock()
            log_mock.reset_mock()

            create_list = [{'component': 'bios', 'current_version': 'v1.0.0'},
                           {'component': 'bmc', 'current_version': 'v1.0.0'}]
            fw_cmp_list.sync_firmware_components.return_value = (
                create_list, [], []
            )

            bios_component = {'component': 'bios',
                              'current_version': 'v1.0.0',
                              'node_id': self.node.id}

            bmc_component = {'component': 'bmc', 'current_version': 'v1.0.0',
                             'node_id': self.node.id}

            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=True) as task:
                manager_mock.return_value.firmware_version = "v1.0.0"
                system_mock.return_value.bios_version = "v1.0.0"

                netadp_ctrl = mock.MagicMock()
                netadp_ctrl.firmware_package_version = invalid_version
                netadp = mock.MagicMock()
                netadp.identity = 'NIC1'
                netadp.controllers = [netadp_ctrl]
                net_adapters = mock.MagicMock()
                net_adapters.get_members.return_value = [netadp]
                chassis_mock.return_value.network_adapters = net_adapters
                task.driver.firmware.cache_firmware_components(task)

                fw_cmp_list.sync_firmware_components.assert_called_once_with(
                    task.context, task.node.id,
                    [{'component': 'bios', 'current_version': 'v1.0.0'},
                     {'component': 'bmc', 'current_version': 'v1.0.0'}])

                fw_cmp_calls = [
                    mock.call(task.context, **bios_component),
                    mock.call().create(),
                    mock.call(task.context, **bmc_component),
                    mock.call().create()
                ]
                fw_cmp_mock.assert_has_calls(fw_cmp_calls)
                log_mock.warning.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_network_adapters_none(
            self, fw_cmp_list, chassis_mock, manager_mock,
            system_mock, log_mock):
        """Test that None network_adapters is handled gracefully."""
        fw_cmp_list.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            system_mock.return_value.bios_version = '1.0.0'
            manager_mock.return_value.firmware_version = '1.0.0'
            # network_adapters is None
            chassis_mock.return_value.network_adapters = None

            task.driver.firmware.cache_firmware_components(task)

        # Should log at debug level, not warning
        log_mock.debug.assert_any_call(
            'NetworkAdapters not available on chassis for '
            'node %(node_uuid)s',
            {'node_uuid': self.node.uuid}
        )
        log_mock.warning.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_missing_attribute_error(
            self, fw_cmp_list, chassis_mock, manager_mock,
            system_mock, log_mock):
        """Test that MissingAttributeError is handled gracefully."""
        fw_cmp_list.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            system_mock.return_value.bios_version = '1.0.0'
            manager_mock.return_value.firmware_version = '1.0.0'
            # network_adapters raises MissingAttributeError
            type(chassis_mock.return_value).network_adapters = (
                mock.PropertyMock(
                    side_effect=sushy.exceptions.MissingAttributeError))

            task.driver.firmware.cache_firmware_components(task)

        # Should log at debug level, not warning
        log_mock.debug.assert_any_call(
            'NetworkAdapters not available on chassis for '
            'node %(node_uuid)s',
            {'node_uuid': self.node.uuid}
        )
        log_mock.warning.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_retrieve_nic_components_serial_number(
            self, fw_cmp_mock, fw_cmp_list, chassis_mock, manager_mock,
            system_mock, log_mock):
        """Test NIC component retrieval uses serial number for HPE systems.

        HPE systems can have NetworkAdapter IDs that change after reboot,
        so we use the SerialNumber when available for stable identification.
        """
        create_list = [
            {'component': 'bios', 'current_version': 'v1.0.0'},
            {'component': 'bmc', 'current_version': 'v1.0.0'},
            {'component': 'nic:SN12345', 'current_version': '1.2.3'},
            {'component': 'nic:NIC2', 'current_version': '1.2.4'}
        ]
        fw_cmp_list.sync_firmware_components.return_value = (
            create_list, [], []
        )

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            manager_mock.return_value.firmware_version = "v1.0.0"
            system_mock.return_value.bios_version = "v1.0.0"

            # First NIC: has serial number - should use serial number
            netadp_ctrl1 = mock.MagicMock()
            netadp_ctrl1.firmware_package_version = "1.2.3"
            netadp1 = mock.MagicMock()
            netadp1.identity = 'NIC1'
            netadp1.serial_number = 'SN12345'
            netadp1.controllers = [netadp_ctrl1]

            # Second NIC: no serial number - should fall back to identity
            netadp_ctrl2 = mock.MagicMock()
            netadp_ctrl2.firmware_package_version = "1.2.4"
            netadp2 = mock.MagicMock()
            netadp2.identity = 'NIC2'
            netadp2.serial_number = None
            netadp2.controllers = [netadp_ctrl2]

            net_adapters = mock.MagicMock()
            net_adapters.get_members.return_value = [netadp1, netadp2]
            chassis_mock.return_value.network_adapters = net_adapters

            task.driver.firmware.cache_firmware_components(task)

            # Verify components include serial number for first NIC
            fw_cmp_list.sync_firmware_components.assert_called_once_with(
                task.context, task.node.id,
                [{'component': 'bios', 'current_version': 'v1.0.0'},
                 {'component': 'bmc', 'current_version': 'v1.0.0'},
                 {'component': 'nic:SN12345', 'current_version': '1.2.3'},
                 {'component': 'nic:NIC2', 'current_version': '1.2.4'}])

            # Verify debug log was called for serial number usage
            log_mock.debug.assert_any_call(
                'Using SerialNumber %(serial_number)s for '
                'NetworkAdapter %(net_adp_id)s',
                {'serial_number': 'SN12345', 'net_adp_id': 'NIC1'})


    @mock.patch.object(redfish_utils, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, '_get_connection', autospec=True)
    def test_missing_updateservice(self, conn_mock, log_mock):
        settings = [{'component': 'bmc', 'url': 'http://upfwbmc/v2.0.0'}]
        conn_mock.side_effect = sushy.exceptions.MissingAttributeError(
            attribute='UpdateService', resource='redfish/v1')
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            error_msg = ('The attribute UpdateService is missing from the '
                         'resource redfish/v1')
            self.assertRaisesRegex(
                exception.RedfishError, error_msg,
                task.driver.firmware.update,
                task, settings)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_missing_simple_update_action(self, get_systems_collection_mock,
                                          update_service_mock, log_mock):
        settings = [{'component': 'bmc', 'url': 'http://upfwbmc/v2.0.0'}]
        update_service = update_service_mock.return_value
        update_service.simple_update.side_effect = \
            sushy.exceptions.MissingAttributeError(
                attribute='#UpdateService.SimpleUpdate',
                resource='redfish/v1/UpdateService')
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:

            self.assertRaises(
                exception.RedfishError,
                task.driver.firmware.update,
                task, settings)
            expected_err_msg = (
                'The attribute #UpdateService.SimpleUpdate is missing '
                'from the resource redfish/v1/UpdateService')
            log_mock.error.assert_called_once_with(
                'The attribute #UpdateService.SimpleUpdate is missing '
                'on node %(node)s. Error: %(error)s',
                {'node': self.node.uuid, 'error': expected_err_msg})

            component = settings[0].get('component')
            url = settings[0].get('url')

            log_call = [
                mock.call('Updating Firmware on node %(node_uuid)s '
                          'with settings %(settings)s, '
                          'allow_grouping_reboots=%(group)s',
                          {'node_uuid': self.node.uuid,
                           'settings': settings,
                           'group': False}),
                mock.call('For node %(node)s serving firmware for '
                          '%(component)s from original location %(url)s',
                          {'node': self.node.uuid,
                           'component': component, 'url': url}),
                mock.call('Applying new firmware %(url)s for '
                          '%(component)s on node %(node_uuid)s',
                          {'url': url, 'component': component,
                           'node_uuid': self.node.uuid})
            ]
            log_mock.debug.assert_has_calls(log_call)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def _test_invalid_settings(self, log_mock):
        step = self.node.clean_step
        settings = step['argsinfo'].get('settings', None)
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaises(
                exception.InvalidParameterValue,
                task.driver.firmware.update,
                task, settings)
            log_mock.debug.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def _test_invalid_settings_service(self, log_mock):
        step = self.node.service_step
        settings = step['argsinfo'].get('settings', None)
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaises(
                exception.InvalidParameterValue,
                task.driver.firmware.update,
                task, settings)
            log_mock.debug.assert_not_called()

    def test_invalid_component_in_settings(self):
        argsinfo = {'settings': [
            {'component': 'something', 'url': 'https://nic-update/v1.1.0'}
        ]}
        self.node.clean_step = {'priority': 100, 'interface': 'firmware',
                                'step': 'update',
                                'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings()

    def test_invalid_component_in_settings_service(self):
        argsinfo = {'settings': [
            {'component': 'something', 'url': 'https://nic-update/v1.1.0'}
        ]}
        self.node.service_step = {'priority': 100, 'interface': 'firmware',
                                  'step': 'update',
                                  'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings_service()

    def test_missing_required_field_in_settings(self):
        argsinfo = {'settings': [
            {'url': 'https://nic-update/v1.1.0'},
            {'component': "bmc"}
        ]}
        self.node.clean_step = {'priority': 100, 'interface': 'firmware',
                                'step': 'update',
                                'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings()

    def test_missing_required_field_in_settings_service(self):
        argsinfo = {'settings': [
            {'url': 'https://nic-update/v1.1.0'},
            {'component': "bmc"}
        ]}
        self.node.service_step = {'priority': 100, 'interface': 'firmware',
                                  'step': 'update',
                                  'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings_service()

    def test_empty_settings(self):
        argsinfo = {'settings': []}
        self.node.clean_step = {'priority': 100, 'interface': 'firmware',
                                'step': 'update',
                                'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings()

    def test_empty_settings_service(self):
        argsinfo = {'settings': []}
        self.node.service_step = {'priority': 100, 'interface': 'firmware',
                                  'step': 'update',
                                  'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings_service()

    @mock.patch.object(time, 'sleep', lambda seconds: None)
    def _generate_new_driver_internal_info(self, components=[], invalid=False,
                                           add_wait=False, wait=1):
        bmc_component = {'component': 'bmc', 'url': 'https://bmc/v1.0.1'}
        bios_component = {'component': 'bios', 'url': 'https://bios/v1.0.1'}
        if add_wait:
            wait_start_time = timeutils.utcnow() -\
                datetime.timedelta(minutes=1)
            bmc_component['wait_start_time'] = wait_start_time.isoformat()
            bios_component['wait_start_time'] = wait_start_time.isoformat()
            bmc_component['wait'] = wait
            bios_component['wait'] = wait

        self.node.clean_step = {'priority': 100, 'interface': 'bios',
                                'step': 'apply_configuration',
                                'argsinfo': {'settings': []}}

        updates = []
        if 'bmc' in components:
            self.node.clean_step['argsinfo']['settings'].append(
                bmc_component)
            bmc_component['task_monitor'] = '/task/1'
            updates.append(bmc_component)
        if 'bios' in components:
            self.node.clean_step['argsinfo']['settings'].append(
                bios_component)
            bios_component['task_monitor'] = '/task/2'
            updates.append(bios_component)

        if invalid:
            self.node.provision_state = states.CLEANING
            self.node.driver_internal_info = {'something': 'else'}
        else:
            self.node.provision_state = states.CLEANING
            self.node.driver_internal_info = {
                'redfish_fw_updates': updates,
            }
        self.node.save()

    def _generate_new_driver_internal_info_service(self, components=[],
                                                   invalid=False,
                                                   add_wait=False, wait=1):
        bmc_component = {'component': 'bmc', 'url': 'https://bmc/v1.0.1'}
        bios_component = {'component': 'bios', 'url': 'https://bios/v1.0.1'}
        if add_wait:
            wait_start_time = timeutils.utcnow() -\
                datetime.timedelta(minutes=1)
            bmc_component['wait_start_time'] = wait_start_time.isoformat()
            bios_component['wait_start_time'] = wait_start_time.isoformat()
            bmc_component['wait'] = wait
            bios_component['wait'] = wait

        self.node.service_step = {'priority': 100, 'interface': 'bios',
                                  'step': 'apply_configuration',
                                  'argsinfo': {'settings': []}}

        updates = []
        if 'bmc' in components:
            self.node.service_step['argsinfo']['settings'].append(
                bmc_component)
            bmc_component['task_monitor'] = '/task/1'
            updates.append(bmc_component)
        if 'bios' in components:
            self.node.service_step['argsinfo']['settings'].append(
                bios_component)
            bios_component['task_monitor'] = '/task/2'
            updates.append(bios_component)

        if invalid:
            self.node.provision_state = states.SERVICING
            self.node.driver_internal_info = {'something': 'else'}
        else:
            self.node.provision_state = states.SERVICING
            self.node.driver_internal_info = {
                'redfish_fw_updates': updates,
            }
        self.node.save()

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def _test__query_methods(self, acquire_mock):
        firmware = redfish_fw.RedfishFirmware()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '',
                      self.node.driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(firmware=firmware))
        acquire_mock.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))

        firmware._check_node_redfish_firmware_update = mock.Mock()
        firmware._clear_updates = mock.Mock()

        # _query_update_status
        firmware._query_update_status(mock_manager, self.context)
        if not self.node.driver_internal_info.get('redfish_fw_updates'):
            firmware._check_node_redfish_firmware_update.assert_not_called()
        else:
            firmware._check_node_redfish_firmware_update.\
                assert_called_once_with(task)

        # _query_update_failed
        firmware._query_update_failed(mock_manager, self.context)
        if not self.node.driver_internal_info.get('redfish_fw_updates'):
            firmware._clear_updates.assert_not_called()
        else:
            firmware._clear_updates.assert_called_once_with(self.node)

    def test_redfish_fw_updates(self):
        self._generate_new_driver_internal_info(['bmc'])
        self._test__query_methods()

    def test_redfish_fw_updates_empty(self):
        self._generate_new_driver_internal_info(invalid=True)
        self._test__query_methods()

    def _test__check_node_redfish_firmware_update(self):
        firmware = redfish_fw.RedfishFirmware()
        firmware._continue_updates = mock.Mock()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.upgrade_lock = mock.Mock()
            task.process_event = mock.Mock()
            firmware._check_node_redfish_firmware_update(task)
            return task, firmware

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test_check_calls_touch_provisioning(self, mock_task_monitor,
                                            mock_get_update_service):
        """Test _check_node_redfish_firmware_update calls touch_provisioning.

        This prevents heartbeat timeouts for firmware updates that don't
        require the ramdisk agent (requires_ramdisk=False). By calling
        touch_provisioning on each poll, we keep provision_updated_at fresh.
        """
        self._generate_new_driver_internal_info(['bmc'])

        # Mock task still in progress
        mock_task_monitor.return_value.is_processing = True
        mock_task_monitor.return_value.get_task.return_value.task_state = \
            sushy.TASK_STATE_RUNNING

        firmware = redfish_fw.RedfishFirmware()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            with mock.patch.object(task.node, 'touch_provisioning',
                                   autospec=True) as mock_touch:
                firmware._check_node_redfish_firmware_update(task)

                # Verify touch_provisioning was called
                mock_touch.assert_called_once_with()

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_skips_touch_provisioning_on_conn_error(
            self, mock_get_update_service):
        """Test touch_provisioning is NOT called when BMC connection fails.

        When the BMC is unresponsive, we should NOT update
        provision_updated_at. This ensures the process eventually times
        out if the BMC never recovers, rather than being kept alive.
        """
        self._generate_new_driver_internal_info(['bmc'])

        # Mock connection error
        mock_get_update_service.side_effect = exception.RedfishConnectionError(
            'Connection failed')

        firmware = redfish_fw.RedfishFirmware()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            with mock.patch.object(task.node, 'touch_provisioning',
                                   autospec=True) as mock_touch:
                firmware._check_node_redfish_firmware_update(task)

                # Verify touch_provisioning was NOT called on connection error
                mock_touch.assert_not_called()

    @mock.patch.object(redfish_fw.manager_utils, 'cleaning_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_overall_timeout_exceeded(self, mock_get_update_service,
                                            mock_error_handler):
        """Test firmware update fails when overall timeout is exceeded.

        This ensures firmware updates don't run indefinitely - if the
        overall timeout is exceeded, the update should fail with an error.
        Uses clean_step so the error is routed to cleaning_error_handler,
        verifying the fix where _check_overall_timeout unconditionally
        called servicing_error_handler.
        """
        self._generate_new_driver_internal_info(['bmc'])
        self.node.provision_state = states.CLEANING

        # Set start time to 3 hours ago (exceeds 2 hour default timeout)
        past_time = (timeutils.utcnow()
                     - datetime.timedelta(hours=3)).isoformat()
        self.node.set_driver_internal_info('redfish_fw_update_start_time',
                                           past_time)
        self.node.save()

        firmware = redfish_fw.RedfishFirmware()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            firmware._check_node_redfish_firmware_update(task)

            # Verify error handler was called with timeout message
            mock_error_handler.assert_called_once()
            call_args = mock_error_handler.call_args
            self.assertIn('exceeded', call_args[0][1].lower())
            self.assertIn('timeout', call_args[0][1].lower())

            # Verify the firmware update info was cleaned up
            task.node.refresh()
            self.assertIsNone(
                task.node.driver_internal_info.get('redfish_fw_updates'))
            self.assertIsNone(
                task.node.driver_internal_info.get(
                    'redfish_fw_update_start_time'))

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test_check_overall_timeout_not_exceeded(self, mock_task_monitor,
                                                mock_get_update_service):
        """Test firmware update continues when timeout not exceeded."""
        self._generate_new_driver_internal_info(['bmc'])

        # Set start time to 1 hour ago (within 2 hour default timeout)
        past_time = (timeutils.utcnow()
                     - datetime.timedelta(hours=1)).isoformat()
        self.node.set_driver_internal_info('redfish_fw_update_start_time',
                                           past_time)
        self.node.save()

        # Mock task still in progress
        mock_task_monitor.return_value.is_processing = True
        mock_task_monitor.return_value.get_task.return_value.task_state = \
            sushy.TASK_STATE_RUNNING

        firmware = redfish_fw.RedfishFirmware()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            with mock.patch.object(task.node, 'touch_provisioning',
                                   autospec=True) as mock_touch:
                firmware._check_node_redfish_firmware_update(task)

                # Verify touch_provisioning was called (update continues)
                mock_touch.assert_called_once_with()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_conn_error(self, get_us_mock, log_mock):
        self._generate_new_driver_internal_info(['bmc'])
        get_us_mock.side_effect = exception.RedfishConnectionError('Error')
        try:
            self._test__check_node_redfish_firmware_update()
        except exception.RedfishError as e:
            exception_error = e.kwargs.get('error')

            warning_calls = [
                mock.call('Unable to communicate with firmware update '
                          'service on node %(node)s. Will try again on '
                          'the next poll. Error: %(error)s',
                          {'node': self.node.uuid,
                           'error': exception_error})
            ]
            log_mock.warning.assert_has_calls(warning_calls)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_update_wait_elapsed(self, get_us_mock, log_mock):
        mock_update_service = mock.Mock()
        get_us_mock.return_value = mock_update_service
        self._generate_new_driver_internal_info(['bmc'], add_wait=True)

        firmware = redfish_fw.RedfishFirmware()
        firmware._handle_wait_completion = mock.Mock()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.upgrade_lock = mock.Mock()
            firmware._check_node_redfish_firmware_update(task)

            debug_calls = [
                mock.call('Finished waiting after firmware update '
                          '%(firmware_image)s on node %(node)s. '
                          'Elapsed time: %(seconds)s seconds',
                          {'firmware_image': 'https://bmc/v1.0.1',
                           'node': self.node.uuid, 'seconds': 60})]
            log_mock.debug.assert_has_calls(debug_calls)
            firmware._handle_wait_completion.assert_called_once()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_update_still_waiting(self, get_us_mock, log_mock):
        mock_update_service = mock.Mock()
        get_us_mock.return_value = mock_update_service
        self._generate_new_driver_internal_info(
            ['bios'], add_wait=True, wait=600)

        _, interface = self._test__check_node_redfish_firmware_update()
        debug_calls = [
            mock.call('Continuing to wait after firmware update '
                      '%(firmware_image)s on node %(node)s. '
                      'Elapsed time: %(seconds)s seconds',
                      {'firmware_image': 'https://bios/v1.0.1',
                       'node': self.node.uuid, 'seconds': 60})]
        log_mock.debug.assert_has_calls(debug_calls)
        interface._continue_updates.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test_check_update_task_monitor_not_found(self, tm_mock, get_us_mock,
                                                 log_mock):
        tm_mock.side_effect = exception.RedfishError()
        self._generate_new_driver_internal_info(['bios'])

        task, interface = self._test__check_node_redfish_firmware_update()
        warning_calls = [
            mock.call('Firmware update completed for node %(node)s, '
                      'firmware %(firmware_image)s, but success of the '
                      'update is unknown.  Assuming update was successful.',
                      {'node': self.node.uuid,
                       'firmware_image': 'https://bios/v1.0.1'})]

        log_mock.warning.assert_has_calls(warning_calls)
        # No component-specific reboot handling; just continues
        interface._continue_updates.assert_called_once()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test_check_update_task_monitor_not_found_bmc(self, tm_mock,
                                                     get_us_mock, log_mock):
        tm_mock.side_effect = exception.RedfishError()
        self._generate_new_driver_internal_info(['bmc'])

        task, interface = self._test__check_node_redfish_firmware_update()

        # Non-BIOS: should call _continue_updates directly
        interface._continue_updates.assert_called_once_with(
            task, get_us_mock.return_value,
            [{'component': 'bmc', 'url': 'https://bmc/v1.0.1',
              'task_monitor': '/task/1'}]
        )

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test_check_update_task_monitor_not_found_bios_already_rebooted(
            self, tm_mock, get_us_mock, power_mock, log_mock):
        tm_mock.side_effect = exception.RedfishError()
        self._generate_new_driver_internal_info(['bios'])
        # Simulate reboot already triggered on previous poll
        settings = self.node.driver_internal_info['redfish_fw_updates']
        settings[0]['bios_reboot_triggered'] = True
        self.node.set_driver_internal_info('redfish_fw_updates', settings)
        self.node.save()

        task, interface = self._test__check_node_redfish_firmware_update()

        # Reboot already done: should fall through to _continue_updates
        power_mock.assert_not_called()
        interface._continue_updates.assert_called_once()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_update_in_progress(self, tm_mock, get_us_mock, log_mock):
        tm_mock.return_value.is_processing = True
        task_mock = mock.Mock()
        task_mock.task_state = sushy.TASK_STATE_RUNNING
        tm_mock.return_value.get_task.return_value = task_mock

        self._generate_new_driver_internal_info(['bmc'])

        _, interface = self._test__check_node_redfish_firmware_update()
        debug_calls = [
            mock.call('Firmware update in progress for node %(node)s, '
                      'firmware %(firmware_image)s.',
                      {'node': self.node.uuid,
                       'firmware_image': 'https://bmc/v1.0.1'})]

        log_mock.debug.assert_has_calls(debug_calls)

        interface._continue_updates.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_update_task_state(self, tm_mock, get_us_mock, log_mock):
        """Test task with is_processing=False but still in active state.

        Some BMCs (particularly HPE iLO) may return is_processing=False
        while the task is still in RUNNING, STARTING, or PENDING state.
        The update should continue polling and not be treated as complete.
        """
        self._generate_new_driver_internal_info(['bmc'])

        # Test each of the three active states
        for task_state in [sushy.TASK_STATE_RUNNING,
                           sushy.TASK_STATE_STARTING,
                           sushy.TASK_STATE_PENDING]:
            log_mock.reset_mock()

            tm_mock.return_value.is_processing = False
            mock_task = tm_mock.return_value.get_task.return_value
            mock_task.task_state = task_state
            mock_task.task_status = sushy.HEALTH_OK

            _, interface = self._test__check_node_redfish_firmware_update()

            # Verify the new debug log message
            debug_calls = [
                mock.call('Firmware update in progress for node %(node)s, '
                          'firmware %(firmware_image)s.',
                          {'node': self.node.uuid,
                           'firmware_image': 'https://bmc/v1.0.1'})]

            log_mock.debug.assert_has_calls(debug_calls)
            interface._continue_updates.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_update_task_state_new(self, tm_mock, get_us_mock,
                                          log_mock):
        """Test task in NEW state is treated as in-progress, not terminal.

        Dell iDRAC returns TaskState.NEW immediately after a SimpleUpdate
        request before the task transitions to STARTING. This must not be
        treated as a terminal state, otherwise the firmware update is
        incorrectly declared as failed.
        """
        self._generate_new_driver_internal_info(['bmc'])

        tm_mock.return_value.is_processing = False
        mock_task = tm_mock.return_value.get_task.return_value
        mock_task.task_state = sushy.TASK_STATE_NEW
        mock_task.task_status = sushy.HEALTH_OK

        _, interface = self._test__check_node_redfish_firmware_update()

        debug_calls = [
            mock.call('Firmware update in progress for node %(node)s, '
                      'firmware %(firmware_image)s.',
                      {'node': self.node.uuid,
                       'firmware_image': 'https://bmc/v1.0.1'})]

        log_mock.debug.assert_has_calls(debug_calls)
        interface._continue_updates.assert_not_called()

    @mock.patch.object(manager_utils, 'cleaning_error_handler', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_fail(self, tm_mock, get_us_mock,
                                              cleaning_error_handler_mock):
        mock_sushy_task = mock.Mock()
        mock_sushy_task.task_state = 'exception'
        mock_message_unparsed = mock.Mock()
        mock_message_unparsed.message = None
        message_mock = mock.Mock()
        message_mock.message = 'Firmware upgrade failed'
        messages = mock.MagicMock(return_value=[[mock_message_unparsed],
                                                [message_mock],
                                                [message_mock]])
        mock_sushy_task.messages = messages
        mock_task_monitor = mock.Mock()
        mock_task_monitor.is_processing = False
        mock_task_monitor.get_task.return_value = mock_sushy_task
        tm_mock.return_value = mock_task_monitor
        self._generate_new_driver_internal_info(['bmc'])

        task, interface = self._test__check_node_redfish_firmware_update()

        task.upgrade_lock.assert_called_once_with()
        cleaning_error_handler_mock.assert_called_once()
        interface._continue_updates.assert_not_called()

    @mock.patch.object(manager_utils, 'servicing_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_fail_servicing(
            self, tm_mock,
            get_us_mock,
            servicing_error_handler_mock):

        mock_sushy_task = mock.Mock()
        mock_sushy_task.task_state = 'exception'
        mock_message_unparsed = mock.Mock()
        mock_message_unparsed.message = None
        message_mock = mock.Mock()
        message_mock.message = 'Firmware upgrade failed'
        messages = mock.MagicMock(return_value=[[mock_message_unparsed],
                                                [message_mock],
                                                [message_mock]])
        mock_sushy_task.messages = messages
        mock_task_monitor = mock.Mock()
        mock_task_monitor.is_processing = False
        mock_task_monitor.get_task.return_value = mock_sushy_task
        tm_mock.return_value = mock_task_monitor
        self._generate_new_driver_internal_info_service(['bmc'])

        task, interface = self._test__check_node_redfish_firmware_update()

        task.upgrade_lock.assert_called_once_with()
        servicing_error_handler_mock.assert_called_once()
        interface._continue_updates.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_handle_bmc_update_completion', autospec=True)
    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_done(self, tm_mock, get_us_mock,
                                              log_mock,
                                              bmc_completion_mock):
        task_mock = mock.Mock()
        task_mock.task_state = sushy.TASK_STATE_COMPLETED
        task_mock.task_status = sushy.HEALTH_OK
        message_mock = mock.Mock()
        message_mock.message = 'Firmware update done'
        task_mock.messages = [message_mock]
        mock_task_monitor = mock.Mock()
        mock_task_monitor.is_processing = False
        mock_task_monitor.get_task.return_value = task_mock
        tm_mock.return_value = mock_task_monitor
        self._generate_new_driver_internal_info(['bmc'])

        task, interface = self._test__check_node_redfish_firmware_update()
        task.upgrade_lock.assert_called_once_with()
        debug_calls = [
            mock.call('Redfish task completed for node %(node)s, '
                      'firmware %(firmware_image)s: %(messages)s.',
                      {'node': self.node.uuid,
                       'firmware_image': 'https://bmc/v1.0.1',
                       'messages': 'Firmware update done'})]

        log_mock.debug.assert_has_calls(debug_calls)
        # NOTE(iurygregory): _validate_resources_stability is now called
        # in _continue_updates before power operations, not in
        # _handle_task_completion

        # BMC updates now go through _handle_bmc_update_completion
        bmc_completion_mock.assert_called_once_with(
            interface, task, get_us_mock.return_value,
            [{'component': 'bmc', 'url': 'https://bmc/v1.0.1',
              'task_monitor': '/task/1'}],
            {'component': 'bmc', 'url': 'https://bmc/v1.0.1',
             'task_monitor': '/task/1'}
        )

    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    @mock.patch.object(firmware_utils, 'stage', autospec=True)
    def test__stage_firmware_file_https(self, stage_mock, dwl_tmp_mock):
        CONF.set_override('firmware_source', 'local', 'redfish')
        firmware_update = {'url': 'https://test1', 'component': 'bmc'}
        node = mock.Mock()
        dwl_tmp_mock.return_value = '/tmp/test1'
        stage_mock.return_value = ('http://staged/test1', 'http')

        firmware = redfish_fw.RedfishFirmware()

        staged_url, needs_cleanup = firmware._stage_firmware_file(
            node, firmware_update)

        self.assertEqual(staged_url, 'http://staged/test1')
        self.assertEqual(needs_cleanup, 'http')
        dwl_tmp_mock.assert_called_with(node, 'https://test1')
        stage_mock.assert_called_with(node, 'local', '/tmp/test1')

    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    @mock.patch.object(firmware_utils, 'stage', autospec=True)
    @mock.patch.object(firmware_utils, 'get_swift_temp_url', autospec=True)
    def test__stage_firmware_file_swift(
            self, get_swift_tmp_url_mock, stage_mock, dwl_tmp_mock):
        CONF.set_override('firmware_source', 'swift', 'redfish')
        firmware_update = {'url': 'swift://container/bios.exe',
                           'component': 'bios'}
        node = mock.Mock()
        get_swift_tmp_url_mock.return_value = 'http://temp'

        firmware = redfish_fw.RedfishFirmware()

        staged_url, needs_cleanup = firmware._stage_firmware_file(
            node, firmware_update)

        self.assertEqual(staged_url, 'http://temp')
        self.assertIsNone(needs_cleanup)
        dwl_tmp_mock.assert_not_called()
        stage_mock.assert_not_called()

    @mock.patch.object(firmware_utils, 'cleanup', autospec=True)
    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    @mock.patch.object(firmware_utils, 'stage', autospec=True)
    def test__stage_firmware_file_error(self, stage_mock, dwl_tmp_mock,
                                        cleanup_mock):
        CONF.set_override('firmware_source', 'local', 'redfish')
        node = mock.Mock()
        firmware_update = {'url': 'https://test1', 'component': 'bmc'}
        dwl_tmp_mock.return_value = '/tmp/test1'
        stage_mock.side_effect = exception.IronicException

        firmware = redfish_fw.RedfishFirmware()
        self.assertRaises(exception.IronicException,
                          firmware._stage_firmware_file, node,
                          firmware_update)
        dwl_tmp_mock.assert_called_with(node, 'https://test1')
        stage_mock.assert_called_with(node, 'local', '/tmp/test1')
        cleanup_mock.assert_called_with(node)

    def _test_continue_updates(self):

        update_service_mock = mock.Mock()
        firmware = redfish_fw.RedfishFirmware()

        updates = self.node.driver_internal_info.get('redfish_fw_updates')

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            firmware._continue_updates(
                task,
                update_service_mock,
                updates
            )
            return task

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test_continue_update_waitting(self, log_mock):
        self._generate_new_driver_internal_info(['bmc', 'bios'],
                                                add_wait=True, wait=120)
        self._test_continue_updates()
        debug_call = [
            mock.call('Waiting at %(time)s for %(seconds)s seconds '
                      'after %(component)s firmware update %(url)s '
                      'on node %(node)s',
                      {'time': mock.ANY, 'seconds': 120,
                       'component': 'bmc', 'url': 'https://bmc/v1.0.1',
                       'node': self.node.uuid})
        ]
        log_mock.debug.assert_has_calls(debug_call)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    def test_continue_updates_last(self, cond_resume_clean_mock, log_mock,
                                   validate_mock):
        self._generate_new_driver_internal_info(['bmc'])
        task = self._test_continue_updates()

        cond_resume_clean_mock.assert_called_once_with(task)
        # Verify BMC validation was called before resuming conductor
        validate_mock.assert_called_once()

        info_call = [
            mock.call('Firmware updates completed for node %(node)s',
                      {'node': self.node.uuid})
        ]
        log_mock.info.assert_has_calls(info_call)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_service',
                       autospec=True)
    def test_continue_updates_last_service(self, cond_resume_service_mock,
                                           log_mock, validate_mock):
        self._generate_new_driver_internal_info_service(['bmc'])
        task = self._test_continue_updates()

        cond_resume_service_mock.assert_called_once_with(task)
        # Verify BMC validation was called before resuming conductor
        validate_mock.assert_called_once()

        info_call = [
            mock.call('Firmware updates completed for node %(node)s',
                      {'node': self.node.uuid})
        ]
        log_mock.info.assert_has_calls(info_call)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test_continue_updates_more_updates(self, log_mock,
                                           validate_mock,
                                           mock_execute_batched):
        self._generate_new_driver_internal_info(['bmc', 'bios'])

        update_service_mock = mock.Mock()

        firmware = redfish_fw.RedfishFirmware()
        updates = self.node.driver_internal_info.get('redfish_fw_updates')

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.node.save = mock.Mock()

            firmware._continue_updates(task, update_service_mock, updates)

            # After popping BMC, remaining [bios] enters batch path
            mock_execute_batched.assert_called_once()
            batched_settings = mock_execute_batched.call_args[0][3]
            self.assertEqual(1, len(batched_settings))
            self.assertEqual('bios', batched_settings[0]['component'])
            # Verify BMC validation was called before continuing to next update
            validate_mock.assert_called_once_with(firmware, task.node)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    @mock.patch.object(time, 'sleep', lambda seconds: None)
    def test__submit_simple_update_no_targets(self,
                                              get_system_collection_mock,
                                              system_mock):
        self._generate_new_driver_internal_info(['bios'])
        with open('ironic/tests/json_samples/'
                  'systems_collection_single.json') as f:
            response_obj = json.load(f)
        system_collection_mock = mock.MagicMock()
        system_collection_mock.get_members.return_value = response_obj[
            'Members']
        get_system_collection_mock.return_value = system_collection_mock

        task_monitor_mock = mock.Mock()
        task_monitor_mock.task_monitor_uri = '/task/2'
        update_service_mock = mock.Mock()
        update_service_mock.simple_update.return_value = task_monitor_mock
        firmware = redfish_fw.RedfishFirmware()

        fw_upd = {'component': 'bios', 'url': 'https://bios/v1.0.1'}
        firmware._submit_simple_update(self.node, update_service_mock,
                                       fw_upd)
        update_service_mock.simple_update.assert_called_once_with(
            'https://bios/v1.0.1')

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    @mock.patch.object(time, 'sleep', lambda seconds: None)
    def test__submit_simple_update_targets(self,
                                           get_system_collection_mock,
                                           system_mock):
        self._generate_new_driver_internal_info(['bios'])
        with open('ironic/tests/json_samples/'
                  'systems_collection_dual.json') as f:
            response_obj = json.load(f)
        system_collection_mock = mock.MagicMock()
        system_collection_mock.members_identities = response_obj[
            'Members']
        get_system_collection_mock.return_value = system_collection_mock

        task_monitor_mock = mock.Mock()
        task_monitor_mock.task_monitor_uri = '/task/2'
        update_service_mock = mock.Mock()
        update_service_mock.simple_update.return_value = task_monitor_mock
        firmware = redfish_fw.RedfishFirmware()

        fw_upd = {'component': 'bios', 'url': 'https://bios/v1.0.1'}
        firmware._submit_simple_update(self.node, update_service_mock,
                                       fw_upd)
        update_service_mock.simple_update.assert_called_once_with(
            'https://bios/v1.0.1', targets=[mock.ANY])

    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test__submit_simple_update_bmc(self, get_sys_collec_mock):
        self._generate_new_driver_internal_info(['bmc'])
        with open(
            'ironic/tests/json_samples/systems_collection_single.json'
        ) as f:
            resp_obj = json.load(f)
        system_collection_mock = mock.MagicMock()
        system_collection_mock.get_members.return_value = resp_obj['Members']
        get_sys_collec_mock.return_value = system_collection_mock

        task_monitor_mock = mock.Mock()
        task_monitor_mock.task_monitor_uri = '/task/2'
        update_service_mock = mock.Mock()
        update_service_mock.simple_update.return_value = task_monitor_mock
        firmware = redfish_fw.RedfishFirmware()
        fw_upd = {'component': 'bmc', 'url': 'https://bmc/v1.2.3'}
        firmware._submit_simple_update(self.node, update_service_mock,
                                       fw_upd)
        update_service_mock.simple_update.assert_called_once_with(
            'https://bmc/v1.2.3')

    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test__submit_simple_update_bmc_node_override(
            self, get_sys_collec_mock):
        self._generate_new_driver_internal_info(['bmc'])
        with open(
            'ironic/tests/json_samples/systems_collection_single.json'
        ) as f:
            resp_obj = json.load(f)
        system_collection_mock = mock.MagicMock()
        system_collection_mock.get_members.return_value = resp_obj['Members']
        get_sys_collec_mock.return_value = system_collection_mock

        task_monitor_mock = mock.Mock()
        task_monitor_mock.task_monitor_uri = '/task/2'
        update_service_mock = mock.Mock()
        update_service_mock.simple_update.return_value = task_monitor_mock
        firmware = redfish_fw.RedfishFirmware()
        fw_upd = {'component': 'bmc', 'url': 'https://bmc/v1.2.3'}
        firmware._submit_simple_update(self.node, update_service_mock,
                                       fw_upd)
        update_service_mock.simple_update.assert_called_once_with(
            'https://bmc/v1.2.3')

    def test__validate_resources_stability_success(self):
        """Test successful BMC resource validation with consecutive success."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep',
                                   autospec=True) as sleep_mock:

                # Mock successful resource responses
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                net_adapters = chassis_mock.return_value.network_adapters
                net_adapters.get_members.return_value = []

                # Mock time progression to simulate consecutive successes
                time_mock.side_effect = [0, 1, 2, 3]  # 3 successful attempts

                # Should complete successfully after 3 consecutive successes
                firmware._validate_resources_stability(task.node)

                # Verify all resources were checked 3 times (required success)
                self.assertEqual(system_mock.call_count, 3)
                self.assertEqual(manager_mock.call_count, 3)
                self.assertEqual(chassis_mock.call_count, 3)

                # Verify sleep was called between validation attempts
                expected_calls = [mock.call(
                    CONF.redfish.firmware_update_validation_interval)] * 2
                sleep_mock.assert_has_calls(expected_calls)

    def test__validate_resources_stability_timeout(self):
        """Test BMC resource validation timeout when not achieved."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True), \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock system always failing
                system_mock.side_effect = exception.RedfishConnectionError(
                    'timeout')

                # Mock time progression to exceed timeout
                time_mock.side_effect = [0, 500]

                # Should raise RedfishError due to timeout
                self.assertRaises(exception.RedfishError,
                                  firmware._validate_resources_stability,
                                  task.node)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test__validate_resources_stability_intermittent_failures(
            self, mock_log):
        """Test BMC resource validation with intermittent failures."""
        cfg.CONF.set_override('firmware_update_required_successes', 3,
                              'redfish')
        cfg.CONF.set_override('firmware_update_validation_interval', 10,
                              'redfish')

        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock intermittent failures: success, success, fail,
                # success, success, success
                # When system_mock raises exception, other calls are not made
                call_count = 0

                def system_side_effect(*args):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 3:  # Third call fails
                        raise exception.RedfishConnectionError('error')
                    return mock.Mock()

                system_mock.side_effect = system_side_effect
                manager_mock.return_value = mock.Mock()
                net_adapters = chassis_mock.return_value.network_adapters
                net_adapters.get_members.return_value = []

                # Mock time progression (6 attempts total)
                time_mock.side_effect = [0, 10, 20, 30, 40, 50, 60]

                # Should eventually succeed after counter reset
                firmware._validate_resources_stability(task.node)

                # Verify all 6 attempts were made for system
                self.assertEqual(system_mock.call_count, 6)
                # Manager and chassis called only 5 times (not on failed)
                self.assertEqual(manager_mock.call_count, 5)
                self.assertEqual(chassis_mock.call_count, 5)

                # Verify verbose logging about BMC recovery was called
                expected_log_call = mock.call(
                    'BMC resource validation failed for node %(node)s: '
                    '%(error)s. This may indicate the BMC is still '
                    'restarting or recovering from firmware update.',
                    {'node': task.node.uuid, 'error': mock.ANY})
                mock_log.debug.assert_has_calls([expected_log_call])

    def test__validate_resources_stability_manager_failure(self):
        """Test BMC resource validation when Manager resource fails."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock:

                # Mock system success, manager failure
                system_mock.return_value = mock.Mock()
                manager_mock.side_effect = exception.RedfishError(
                    'manager error')

                # Mock time progression to exceed timeout
                time_mock.side_effect = [0, 500]

                # Should raise RedfishError due to timeout
                self.assertRaises(exception.RedfishError,
                                  firmware._validate_resources_stability,
                                  task.node)

    def test__validate_resources_stability_network_adapters_failure(self):
        """Test validation when NetworkAdapters resource fails."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock:

                # Mock system and manager success, NetworkAdapters failure
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                chassis_mock.side_effect = exception.RedfishError(
                    'chassis error')

                # Mock time progression to exceed timeout
                time_mock.side_effect = [0, 500]

                # Should raise RedfishError due to timeout
                self.assertRaises(exception.RedfishError,
                                  firmware._validate_resources_stability,
                                  task.node)

    def test__validate_resources_stability_custom_config(self):
        """Test BMC resource validation with custom configuration values."""
        cfg.CONF.set_override('firmware_update_required_successes', 5,
                              'redfish')
        cfg.CONF.set_override('firmware_update_validation_interval', 5,
                              'redfish')

        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep',
                                   autospec=True) as sleep_mock:

                # Mock successful resource responses
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                net_adapters = chassis_mock.return_value.network_adapters
                net_adapters.get_members.return_value = []

                # Mock time progression (5 successful attempts)
                time_mock.side_effect = [0, 5, 10, 15, 20, 25]

                # Should complete successfully after 5 consecutive successes
                firmware._validate_resources_stability(task.node)

                # Verify all resources checked 5 times (custom required)
                self.assertEqual(system_mock.call_count, 5)
                self.assertEqual(manager_mock.call_count, 5)
                self.assertEqual(chassis_mock.call_count, 5)

                # Verify sleep was called with custom interval
                expected_calls = [mock.call(5)] * 4  # 4 sleeps between 5
                sleep_mock.assert_has_calls(expected_calls)

    def test__validate_resources_stability_network_adapters_none(self):
        """Test validation succeeds when network_adapters is None."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock successful resource responses but network_adapters None
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                chassis_mock.return_value.network_adapters = None

                # Mock time progression to simulate consecutive successes
                time_mock.side_effect = [0, 1, 2, 3]

                # Should complete successfully (None network_adapters is OK)
                firmware._validate_resources_stability(task.node)

                # Verify all resources were checked 3 times
                self.assertEqual(system_mock.call_count, 3)
                self.assertEqual(manager_mock.call_count, 3)
                self.assertEqual(chassis_mock.call_count, 3)

    def test__validate_resources_stability_network_adapters_missing_attr(self):
        """Test validation succeeds when network_adapters is missing."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock successful resource responses
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                # network_adapters raises MissingAttributeError
                type(chassis_mock.return_value).network_adapters = (
                    mock.PropertyMock(
                        side_effect=sushy.exceptions.MissingAttributeError))

                # Mock time progression to simulate consecutive successes
                time_mock.side_effect = [0, 1, 2, 3]

                # Should complete successfully (missing network_adapters is OK)
                firmware._validate_resources_stability(task.node)

                # Verify all resources were checked 3 times
                self.assertEqual(system_mock.call_count, 3)
                self.assertEqual(manager_mock.call_count, 3)
                self.assertEqual(chassis_mock.call_count, 3)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test__validate_resources_stability_badrequest_error(self, mock_log):
        """Test BMC resource validation handles BadRequestError correctly."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock BadRequestError from sushy with proper arguments
                mock_response = mock.Mock()
                mock_response.status_code = 400
                system_mock.side_effect = sushy.exceptions.BadRequestError(
                    'http://test', mock_response, mock_response)

                # Mock time progression: start at 0, try once at 10, timeout
                # at 500, this allows at least one loop iteration to trigger
                # the exception
                time_mock.side_effect = [0, 10, 500]

                # Should raise RedfishError due to timeout
                self.assertRaises(exception.RedfishError,
                                  firmware._validate_resources_stability,
                                  task.node)

                # Verify verbose logging about BMC recovery was called
                expected_log_call = mock.call(
                    'BMC resource validation failed for node %(node)s: '
                    '%(error)s. This may indicate the BMC is still '
                    'restarting or recovering from firmware update.',
                    {'node': task.node.uuid, 'error': mock.ANY})
                mock_log.debug.assert_has_calls([expected_log_call])

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bmc_uses_configured_timeout(self, mock_get_update_service,
                                                mock_submit_simple_update,
                                                mock_set_async_flags,
                                                mock_get_system,
                                                mock_get_manager):
        """Test BMC firmware update sets up version checking."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0'}]

        # Mock system
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system

        # Mock BMC version reading
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # BMC uses version checking, not immediate reboot
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=False,
                polling=True
            )
            # Verify BMC version check tracking is set up
            info = task.node.driver_internal_info
            fw_updates = info.get('redfish_fw_updates', [])
            self.assertEqual(1, len(fw_updates))
            self.assertIn('bmc_check_start_time', fw_updates[0])
            self.assertIn('bmc_fw_version_before_update', info)
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bmc_uses_bmc_constant(self, mock_get_update_service,
                                          mock_submit_simple_update,
                                          mock_set_async_flags,
                                          mock_get_system,
                                          mock_get_manager):
        """Test BMC firmware update detection works with BMC constant."""
        settings = [{'component': redfish_utils.BMC,
                     'url': 'http://bmc/v1.0.0'}]

        # Mock system
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system

        # Mock BMC version reading
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # BMC uses version checking, not immediate reboot
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=False,
                polling=True
            )
            # Verify BMC version check tracking is set up
            info = task.node.driver_internal_info
            fw_updates = info.get('redfish_fw_updates', [])
            self.assertEqual(1, len(fw_updates))
            self.assertIn('bmc_check_start_time', fw_updates[0])
            self.assertIn('bmc_fw_version_before_update', info)
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_non_bmc_uses_wait_parameter(self, mock_get_update_service,
                                                mock_execute_batched):
        """Test non-BMC firmware update uses batched path."""
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            mock_execute_batched.assert_called_once()
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_non_bmc_no_wait_parameter(self, mock_get_update_service,
                                              mock_execute_batched):
        """Test non-BMC firmware update without wait parameter."""
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            mock_execute_batched.assert_called_once()
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_mixed_components_with_bmc(self, mock_get_update_service,
                                              mock_execute_batched):
        """Test mixed component update with BIOS first uses batch path."""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0', 'wait': 60}
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # First component is BIOS, so batch path
            mock_execute_batched.assert_called_once()
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bmc_with_explicit_wait(self, mock_get_update_service,
                                           mock_submit_simple_update,
                                           mock_get_system,
                                           mock_get_manager,
                                           mock_set_async_flags):
        """Test BMC update with explicit wait."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                     'wait': 90}]

        # Mock system
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system

        # Mock BMC version reading
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # BMC uses version checking, not immediate reboot
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=False,
                polling=True
            )
            # Verify wait time is stored
            info = task.node.driver_internal_info
            fw_updates = info['redfish_fw_updates']
            self.assertEqual(90, fw_updates[0]['wait'])
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bmc_no_immediate_reboot(self, mock_get_update_service,
                                            mock_submit_simple_update,
                                            mock_get_system,
                                            mock_get_manager,
                                            mock_set_async_flags):
        """Test BMC firmware update does not set immediate reboot."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0'}]

        # Mock system
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system

        # Mock BMC version reading
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # Verify reboot=False for BMC updates
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=False,
                polling=True
            )
            # Verify we return wait state to keep step active
            self.assertEqual(states.SERVICEWAIT, result)

            # Verify BMC version check tracking is set up
            info = task.node.driver_internal_info
            fw_updates = info.get('redfish_fw_updates', [])
            self.assertEqual(1, len(fw_updates))
            self.assertIn('bmc_check_start_time', fw_updates[0])
            self.assertIn('bmc_fw_version_before_update', info)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_nic_no_immediate_reboot(self, mock_get_update_service,
                                            mock_execute_batched):
        """Test NIC firmware update uses batched path."""
        settings = [{'component': 'nic:BCM57414', 'url': 'http://nic/v1.0.0'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            mock_execute_batched.assert_called_once()
            # Verify we return wait state to keep step active
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bios_sets_reboot_flag(self, mock_get_update_service,
                                          mock_execute_batched):
        """Test BIOS firmware update uses batched path."""
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            # Set up node as if in service step
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            mock_execute_batched.assert_called_once()
            # Verify we return wait state to keep step active
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(timeutils, 'utcnow', autospec=True)
    @mock.patch.object(timeutils, 'parse_isotime', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_get_current_bmc_version', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_bmc_version_check_timeout_sets_reboot_flag(
            self, mock_get_update_service, mock_get_bmc_version,
            mock_set_async_flags, mock_continue_updates,
            mock_parse_isotime, mock_utcnow):
        """Test BMC version check timeout sets reboot request flag."""
        import datetime
        start_time = datetime.datetime(2025, 1, 1, 0, 0, 0,
                                       tzinfo=datetime.timezone.utc)
        current_time = start_time + datetime.timedelta(seconds=301)
        mock_parse_isotime.return_value = start_time
        mock_utcnow.return_value = current_time
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                     'wait': 300, 'task_monitor': '/tasks/1',
                     'bmc_check_start_time': '2025-01-01T00:00:00.000000'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            # Set up node with BMC version checking in progress
            task.node.set_driver_internal_info(
                'redfish_fw_updates', settings)

            # Mock BMC is unresponsive
            mock_get_bmc_version.return_value = None

            # Call the BMC update completion handler
            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._handle_bmc_update_completion(
                task, mock_get_update_service.return_value,
                settings, settings[0])

            # Verify reboot flag is set
            info = task.node.driver_internal_info
            self.assertTrue(info.get('firmware_reboot_requested'))

            # Verify async flags updated with reboot=True
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=True,
                polling=True
            )

            # Verify _continue_updates was called
            mock_continue_updates.assert_called_once()

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_get_current_bmc_version', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_bmc_version_change_with_multiple_components_triggers_reboot(
            self, mock_get_update_service, mock_get_bmc_version,
            mock_continue_updates, mock_power_action):
        """Test BMC version change with multiple components triggers reboot."""
        settings = [
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0',
             'wait': 300, 'task_monitor': '/tasks/1',
             'bmc_check_start_time': '2025-01-01T00:00:00.000000'},
            {'component': 'nic:BCM57414', 'url': 'http://nic/v1.0.0',
             'task_monitor': '/tasks/2'}
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            # Set up node with BMC version before update
            task.node.set_driver_internal_info(
                'bmc_fw_version_before_update', '1.0.0')
            task.node.set_driver_internal_info(
                'redfish_fw_updates', settings)

            # Mock BMC version has changed
            mock_get_bmc_version.return_value = '2.0.0'

            # Call the BMC update completion handler
            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._handle_bmc_update_completion(
                task, mock_get_update_service.return_value,
                settings, settings[0])

            # Verify version check was called
            mock_get_bmc_version.assert_called_once_with(
                firmware_interface, task.node)

            # Verify bmc_update_completed flag is set
            info = task.node.driver_internal_info
            fw_updates = info.get('redfish_fw_updates', [])
            self.assertTrue(fw_updates[0].get('bmc_update_completed'))

            # Verify bmc_fw_version_before_update is removed
            self.assertNotIn('bmc_fw_version_before_update', info)

            # Verify settings were saved
            self.assertEqual(2, len(fw_updates))

            # Verify reboot was triggered
            mock_power_action.assert_called_once_with(task, states.REBOOT)

            # Verify _continue_updates was NOT called (reboot happens first)
            mock_continue_updates.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_get_current_bmc_version', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_bmc_version_change_last_component_continues_updates(
            self, mock_get_update_service, mock_get_bmc_version,
            mock_power_action, mock_continue_updates):
        """Test BMC version change as last component continues updates."""
        settings = [
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0',
             'wait': 300, 'task_monitor': '/tasks/1',
             'bmc_check_start_time': '2025-01-01T00:00:00.000000'}
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            # Set up node with BMC version before update
            task.node.set_driver_internal_info(
                'bmc_fw_version_before_update', '1.0.0')
            task.node.set_driver_internal_info(
                'redfish_fw_updates', settings)

            # Mock BMC version has changed
            mock_get_bmc_version.return_value = '2.0.0'

            # Call the BMC update completion handler
            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._handle_bmc_update_completion(
                task, mock_get_update_service.return_value,
                settings, settings[0])

            # Verify version check was called
            mock_get_bmc_version.assert_called_once_with(
                firmware_interface, task.node)

            # Verify bmc_fw_version_before_update is removed
            info = task.node.driver_internal_info
            self.assertNotIn('bmc_fw_version_before_update', info)

            # Verify bmc_update_completed flag is NOT set (last component)
            fw_updates = info.get('redfish_fw_updates', [])
            self.assertNotIn('bmc_update_completed', fw_updates[0])

            # Verify reboot was NOT triggered (last component)
            mock_power_action.assert_not_called()

            # Verify _continue_updates WAS called (proceeds to completion)
            mock_continue_updates.assert_called_once_with(
                firmware_interface, task,
                mock_get_update_service.return_value, settings)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_final_update_with_reboot_flag_triggers_reboot(
            self, mock_get_update_service, mock_clear_updates,
            mock_power_action, mock_resume_clean, validate_mock):
        """Test final firmware update with reboot flag triggers reboot."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                     'task_monitor': '/tasks/1'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            # Set up node as if in cleaning
            task.node.clean_step = {'step': 'update', 'interface': 'firmware'}

            # Set up final update with reboot requested
            task.node.set_driver_internal_info(
                'redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_reboot_requested', True)

            # Call _continue_updates with last firmware
            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._continue_updates(
                task, mock_get_update_service.return_value, settings)

            # Verify reboot was triggered
            mock_power_action.assert_called_once_with(task, states.REBOOT)

            # Verify BMC validation was called before resuming conductor
            validate_mock.assert_called_once()

            # Verify resume clean was called
            mock_resume_clean.assert_called_once_with(task)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_final_update_without_reboot_flag_no_reboot(
            self, mock_get_update_service, mock_clear_updates,
            mock_power_action, mock_resume_clean, validate_mock):
        """Test final firmware update without reboot flag skips reboot."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                     'task_monitor': '/tasks/1'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            # Set up node as if in cleaning
            task.node.clean_step = {'step': 'update', 'interface': 'firmware'}

            # Set up final update WITHOUT reboot requested
            task.node.set_driver_internal_info(
                'redfish_fw_updates', settings)
            # Don't set firmware_reboot_requested

            # Call _continue_updates with last firmware
            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._continue_updates(
                task, mock_get_update_service.return_value, settings)

            # Verify reboot was NOT triggered
            mock_power_action.assert_not_called()

            # Verify BMC validation was called before resuming conductor
            validate_mock.assert_called_once()

            # Verify resume clean was still called
            mock_resume_clean.assert_called_once_with(task)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_stores_batched_flag(self, mock_get_update_service,
                                        mock_execute_batched):
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0'},
                    {'component': 'nic:NIC1', 'url': 'http://nic/v1.0.0'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.driver.firmware.update(task, settings,
                                        allow_grouping_reboots=True)
            info = task.node.driver_internal_info
            self.assertTrue(info.get('firmware_batched_update'))
            mock_execute_batched.assert_called_once()

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_rejects_non_bool_allow_grouping_reboots(
            self, mock_get_update_service):
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0'}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.firmware.update,
                              task, settings,
                              allow_grouping_reboots='yes')

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_rejects_duplicate_components_batched(
            self, mock_get_update_service):
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0'},
                    {'component': 'bios', 'url': 'http://bios/v2.0.0'}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.firmware.update,
                              task, settings,
                              allow_grouping_reboots=True)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_allows_different_nic_components_batched(
            self, mock_get_update_service, mock_execute_batched):
        settings = [{'component': 'nic:NIC1', 'url': 'http://nic1/v1.0.0'},
                    {'component': 'nic:NIC2', 'url': 'http://nic2/v1.0.0'}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.driver.firmware.update(task, settings,
                                        allow_grouping_reboots=True)
            mock_execute_batched.assert_called_once()

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_rejects_duplicate_across_bmc_batched(
            self, mock_get_update_service):
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0'},
                    {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
                    {'component': 'bios', 'url': 'http://bios/v2.0.0'}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.firmware.update,
                              task, settings,
                              allow_grouping_reboots=True)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_allows_duplicates_without_batching(
            self, mock_get_update_service, mock_execute_batched):
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0'},
                    {'component': 'bios', 'url': 'http://bios/v2.0.0'}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.driver.firmware.update(task, settings,
                                        allow_grouping_reboots=False)
            mock_execute_batched.assert_called_once()

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_rejects_wait_non_bmc_with_batching(
            self, mock_get_update_service):
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0',
                     'wait': 120},
                    {'component': 'nic:NIC.1-1', 'url': 'http://nic/v1.0.0'}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.firmware.update,
                              task, settings,
                              allow_grouping_reboots=True)

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_rejects_wait_non_bmc_without_batching(
            self, mock_get_update_service):
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0',
                     'wait': 120}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.firmware.update,
                              task, settings,
                              allow_grouping_reboots=False)

    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_allows_wait_without_batching(
            self, mock_get_update_service, mock_get_system,
            mock_get_manager, mock_submit_simple_update):
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                     'wait': 300}]
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.driver.firmware.update(task, settings,
                                        allow_grouping_reboots=False)
            mock_submit_simple_update.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_allows_wait_bmc_with_batching(
            self, mock_get_update_service, mock_get_system,
            mock_get_manager, mock_submit_simple_update):
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                     'wait': 300},
                    {'component': 'bios', 'url': 'http://bios/v1.0.0'}]
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.driver.firmware.update(task, settings,
                                        allow_grouping_reboots=True)
            mock_submit_simple_update.assert_called_once()

    @mock.patch.object(firmware_utils, 'cleanup', autospec=True)
    def test_clear_updates_removes_batched_flag(self, mock_cleanup):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.set_driver_internal_info('redfish_fw_updates', [])
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info(
                'firmware_batch_current_index', 1)
            task.node.set_driver_internal_info(
                'firmware_batch_reboot_time', 'fake')
            task.node.save()
            task.driver.firmware._clear_updates(task.node)
            info = task.node.driver_internal_info
            self.assertNotIn('firmware_batched_update', info)
            self.assertNotIn('firmware_batch_submitted', info)
            self.assertNotIn('firmware_batch_current_index', info)
            self.assertNotIn('firmware_batch_reboot_time', info)
            self.assertNotIn('redfish_fw_updates', info)

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_batched_bmc_first_uses_sequential(
            self, mock_get_update_service, mock_submit_simple_update,
            mock_set_async_flags, mock_get_system, mock_get_manager):
        settings = [
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
        ]
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(
                task, settings, allow_grouping_reboots=True)

            self.assertEqual(states.SERVICEWAIT, result)
            mock_submit_simple_update.assert_called_once()
            info = task.node.driver_internal_info
            self.assertTrue(info.get('firmware_batched_update'))

    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_stage_firmware_file',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_execute_batched_non_bmc_updates(
            self, mock_get_update_service, mock_get_sys_collection,
            mock_stage_fw, mock_set_async_flags):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]

        mock_collection = mock.Mock()
        mock_collection.members_identities = ['System1']
        mock_get_sys_collection.return_value = mock_collection
        mock_stage_fw.return_value = ('http://staged/fw', None)

        mock_update_service = mock_get_update_service.return_value
        mock_task_monitor1 = mock.Mock()
        mock_task_monitor1.task_monitor_uri = '/tasks/1'
        mock_update_service.simple_update.return_value = mock_task_monitor1

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._execute_batched_non_bmc_updates(
                task, mock_update_service, settings)

            mock_update_service.simple_update.assert_called_once()
            self.assertEqual('/tasks/1', settings[0]['task_monitor'])
            self.assertNotIn('task_monitor', settings[1])

            info = task.node.driver_internal_info
            self.assertEqual(0, info.get('firmware_batch_current_index'))
            self.assertNotIn('firmware_batch_submitted', info)

            mock_set_async_flags.assert_called_once_with(
                task.node, reboot=False, polling=True)

    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_stage_firmware_file',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_execute_batched_non_bmc_updates_submit_failure(
            self, mock_get_update_service, mock_get_sys_collection,
            mock_stage_fw, mock_set_async_flags):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]

        mock_collection = mock.Mock()
        mock_collection.members_identities = ['System1']
        mock_get_sys_collection.return_value = mock_collection
        mock_stage_fw.return_value = ('http://staged/fw', None)

        mock_update_service = mock_get_update_service.return_value
        mock_update_service.simple_update.side_effect = (
            sushy.exceptions.SushyError(message='BIOS update failed'))

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            firmware_interface = redfish_fw.RedfishFirmware()
            self.assertRaises(
                sushy.exceptions.SushyError,
                firmware_interface._execute_batched_non_bmc_updates,
                task, mock_update_service, settings)

    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_batched_staging_in_progress(
            self, mock_get_update_service, mock_get_task_monitor):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_RUNNING
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_current_index', 0)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            info = task.node.driver_internal_info
            self.assertEqual(0, info.get('firmware_batch_current_index'))
            self.assertNotIn('firmware_batch_submitted', info)

    @mock.patch.object(redfish_fw.RedfishFirmware, '_stage_firmware_file',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_batched_staging_component_staged_more_remaining(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_get_sys_collection, mock_stage_fw):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_STARTING
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        mock_collection = mock.Mock()
        mock_collection.members_identities = ['System1']
        mock_get_sys_collection.return_value = mock_collection
        mock_stage_fw.return_value = ('http://staged/fw', None)

        mock_update_service = mock_get_update_service.return_value
        mock_task_monitor2 = mock.Mock()
        mock_task_monitor2.task_monitor_uri = '/tasks/2'
        mock_update_service.simple_update.return_value = mock_task_monitor2

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_current_index', 0)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_update_service.simple_update.assert_called_once()
            info = task.node.driver_internal_info
            self.assertEqual(1, info.get('firmware_batch_current_index'))
            self.assertNotIn('firmware_batch_submitted', info)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_batched_staging_all_staged_triggers_reboot(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_power_action):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_COMPLETED
        mock_task.task_status = sushy.HEALTH_OK
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_current_index', 1)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            info = task.node.driver_internal_info
            self.assertTrue(info.get('firmware_batch_submitted'))
            self.assertNotIn('firmware_batch_current_index', info)
            self.assertIn('firmware_batch_reboot_time', info)
            mock_power_action.assert_called_once_with(
                task, states.REBOOT, mock.ANY)

    @mock.patch.object(manager_utils, 'servicing_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_batched_staging_task_failed(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_clear_updates, mock_error_handler):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_EXCEPTION
        mock_task.task_status = sushy.HEALTH_CRITICAL
        mock_msg = mock.Mock()
        mock_msg.message = 'Firmware staging failed'
        mock_msg.message_id = 'MSG001'
        mock_task.messages = [mock_msg]
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_current_index', 0)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_clear_updates.assert_called_once()
            mock_error_handler.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_finalize_batched_update', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_batched_post_reboot_too_early(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_finalize):
        """B1 regression: returns immediately when elapsed < status_interval"""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_batch_reboot_time',
                timeutils.utcnow().isoformat())
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_get_task_monitor.assert_not_called()
            mock_finalize.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_finalize_batched_update', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_batched_post_reboot_bmc_not_stable(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_validate_stability, mock_finalize):
        """B1 regression: stability check fails, returns to retry"""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
        ]
        mock_validate_stability.side_effect = exception.RedfishError(
            error='BMC not stable')

        past_time = (timeutils.utcnow()
                     - datetime.timedelta(minutes=5)).isoformat()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_batch_reboot_time', past_time)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_validate_stability.assert_called_once()
            mock_get_task_monitor.assert_not_called()
            mock_finalize.assert_not_called()

    @mock.patch.object(manager_utils, 'notify_conductor_resume_service',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_batched_post_reboot_all_completed(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_cache_fw, mock_validate_stability, mock_clear_updates,
            mock_resume_service):
        """S1: validates resources before caching firmware components."""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        mock_task1 = mock.Mock()
        mock_task1.task_state = sushy.TASK_STATE_COMPLETED
        mock_task1.task_status = sushy.HEALTH_OK
        mock_task2 = mock.Mock()
        mock_task2.task_state = sushy.TASK_STATE_COMPLETED
        mock_task2.task_status = sushy.HEALTH_OK

        mock_monitor1 = mock.Mock()
        mock_monitor1.get_task.return_value = mock_task1
        mock_monitor2 = mock.Mock()
        mock_monitor2.get_task.return_value = mock_task2

        mock_get_task_monitor.side_effect = [mock_monitor1, mock_monitor2]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_cache_fw.assert_called_once()
            mock_clear_updates.assert_called_once()
            mock_resume_service.assert_called_once_with(task)

    @mock.patch.object(manager_utils, 'servicing_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_batched_post_reboot_task_failed(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_clear_updates, mock_error_handler):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        mock_task1 = mock.Mock()
        mock_task1.task_state = sushy.TASK_STATE_COMPLETED
        mock_task1.task_status = sushy.HEALTH_OK
        mock_task2 = mock.Mock()
        mock_task2.task_state = sushy.TASK_STATE_EXCEPTION
        mock_task2.task_status = sushy.HEALTH_CRITICAL
        mock_msg = mock.Mock()
        mock_msg.message = 'Firmware update failed'
        mock_msg.message_id = 'MSG001'
        mock_task2.messages = [mock_msg]

        mock_monitor1 = mock.Mock()
        mock_monitor1.get_task.return_value = mock_task1
        mock_monitor2 = mock.Mock()
        mock_monitor2.get_task.return_value = mock_task2

        mock_get_task_monitor.side_effect = [mock_monitor1, mock_monitor2]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_clear_updates.assert_called_once()
            mock_error_handler.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_finalize_batched_update', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_batched_post_reboot_still_starting(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_finalize):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        mock_task1 = mock.Mock()
        mock_task1.task_state = sushy.TASK_STATE_COMPLETED
        mock_task1.task_status = sushy.HEALTH_OK
        mock_task2 = mock.Mock()
        mock_task2.task_state = sushy.TASK_STATE_STARTING

        mock_monitor1 = mock.Mock()
        mock_monitor1.get_task.return_value = mock_task1
        mock_monitor2 = mock.Mock()
        mock_monitor2.get_task.return_value = mock_task2

        mock_get_task_monitor.side_effect = [mock_monitor1, mock_monitor2]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_finalize.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_check_batched_update_status', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_node_firmware_update_dispatches_batch_submitted(
            self, mock_get_update_service, mock_check_batch):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'redfish_fw_update_start_time',
                timeutils.utcnow().isoformat())
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_node_redfish_firmware_update(task)

            mock_check_batch.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_check_batched_update_status', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_node_firmware_update_dispatches_batch_staging(
            self, mock_get_update_service, mock_check_batch):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_current_index', 0)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'redfish_fw_update_start_time',
                timeutils.utcnow().isoformat())
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_node_redfish_firmware_update(task)

            mock_check_batch.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_check_batched_update_status', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_handle_firmware_update_task', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_node_firmware_update_no_dispatch_without_batched_flag(
            self, mock_get_update_service, mock_handle_task,
            mock_check_batch):
        """S4: stale index without FIRMWARE_BATCHED_UPDATE doesn't dispatch"""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_current_index', 0)
            task.node.set_driver_internal_info(
                'redfish_fw_update_start_time',
                timeutils.utcnow().isoformat())
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_node_redfish_firmware_update(task)

            mock_check_batch.assert_not_called()
            mock_handle_task.assert_called_once()

    def test_leading_batchable_run_all_non_bmc(self):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]
        self.assertEqual(2, redfish_fw._leading_batchable_run(settings))

    def test_leading_batchable_run_bmc_first(self):
        settings = [
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]
        self.assertEqual(0, redfish_fw._leading_batchable_run(settings))

    def test_leading_batchable_run_bmc_middle(self):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]
        self.assertEqual(1, redfish_fw._leading_batchable_run(settings))

    def test_leading_batchable_run_bmc_last(self):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
        ]
        self.assertEqual(2, redfish_fw._leading_batchable_run(settings))

    def test_leading_batchable_run_single_component(self):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
        ]
        self.assertEqual(1, redfish_fw._leading_batchable_run(settings))

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bmc_first_falls_through_to_sequential(
            self, mock_get_update_service, mock_get_system,
            mock_get_manager, mock_submit_simple_update,
            mock_execute_batched):
        """[bmc, bios, nic] — BMC first, run_length=0, sequential."""
        settings = [
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.driver.firmware.update(task, settings,
                                        allow_grouping_reboots=True)
            mock_submit_simple_update.assert_called_once()
            mock_execute_batched.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_single_non_bmc_uses_batch_path(
            self, mock_get_update_service, mock_execute_batched):
        """[bios] — single component, treated as batch of 1."""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.driver.firmware.update(task, settings,
                                        allow_grouping_reboots=True)
            mock_execute_batched.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bios_bmc_nic_batches_leading_bios(
            self, mock_get_update_service, mock_execute_batched):
        """[bios, bmc, nic] — batch bios (run=1), then BMC+NIC sequential."""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.driver.firmware.update(task, settings,
                                        allow_grouping_reboots=True)
            mock_execute_batched.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_resume_step',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_finalize_batched_update_no_remaining(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_cache_fw, mock_validate_stability, mock_clear_updates,
            mock_resume_step):
        """All settings in one batch, nothing remaining after finalize."""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._finalize_batched_update(task)

            mock_validate_stability.assert_called_once()
            mock_cache_fw.assert_called_once()
            mock_clear_updates.assert_called_once()
            mock_resume_step.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_start_next_segment',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_finalize_batched_update_hands_off_remaining(
            self, mock_get_update_service, mock_cache_fw,
            mock_clear_updates, mock_start_next):
        """[bios, nic, bmc] — batch completes [bios, nic], hands off bmc."""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._finalize_batched_update(task)

            mock_start_next.assert_called_once()
            remaining = mock_start_next.call_args[0][3]
            self.assertEqual(1, len(remaining))
            self.assertEqual('bmc', remaining[0]['component'])
            mock_cache_fw.assert_not_called()
            mock_clear_updates.assert_not_called()
            info = task.node.driver_internal_info
            self.assertNotIn('firmware_batch_submitted', info)
            self.assertNotIn('firmware_batch_reboot_time', info)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_continue_updates_enters_batch_after_bmc(
            self, mock_get_update_service, mock_validate_stability,
            mock_execute_batched):
        """After BMC sequential, remaining [bios, nic] enter batch path."""
        settings = [
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            mock_update_service = mock_get_update_service.return_value
            firmware_interface._continue_updates(
                task, mock_update_service, settings)

            mock_execute_batched.assert_called_once()
            batched_settings = mock_execute_batched.call_args[0][3]
            self.assertEqual(2, len(batched_settings))
            self.assertEqual('bios', batched_settings[0]['component'])

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_execute_batched_non_bmc_updates', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_continue_updates_single_remaining_uses_batch_path(
            self, mock_get_update_service, mock_validate_stability,
            mock_execute_batched, mock_power_action):
        """After BMC sequential, single remaining [bios] uses batch path."""
        settings = [
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            mock_update_service = mock_get_update_service.return_value
            firmware_interface._continue_updates(
                task, mock_update_service, settings)

            mock_execute_batched.assert_called_once()

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_finalize_batched_bios_nic_bmc_submits_bmc(
            self, mock_get_update_service, mock_get_system,
            mock_get_manager, mock_cache_fw,
            mock_validate_stability, mock_clear_updates,
            mock_submit_simple_update, mock_power_action):
        """N1 regression: [bios, nic, bmc] -- BMC is submitted, not skipped.

        _finalize_batched_update pops batch [bios, nic], then hands
        [bmc] to _start_next_segment which must submit it via
        _submit_simple_update, NOT skip it.
        """
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
        ]
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._finalize_batched_update(task)

            mock_submit_simple_update.assert_called_once()
            mock_clear_updates.assert_not_called()
            mock_cache_fw.assert_not_called()

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_finalize_batched_bios_nic_bmc_bios2_nic2_three_phases(
            self, mock_get_update_service, mock_get_system,
            mock_get_manager, mock_cache_fw,
            mock_validate_stability, mock_clear_updates,
            mock_submit_simple_update, mock_power_action):
        """N1 regression: three-phase segmentation.

        [bios, nic, bmc, bios2, nic2] -- after batch [bios, nic] completes,
        BMC submitted via sequential path.
        """
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
            {'component': 'bios', 'url': 'http://bios2/v2.0.0'},
            {'component': 'nic:NIC.Slot.2', 'url': 'http://nic2/v2.0.0'},
        ]
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_submitted', True)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._finalize_batched_update(task)

            mock_submit_simple_update.assert_called_once()
            mock_clear_updates.assert_not_called()
            mock_cache_fw.assert_not_called()

    # --- R2 tests: async step flags, error dispatch, cache parity ---

    def _test_batch_reboot_async_flags(self, step_attr, reboot_flag,
                                       polling_flag, mock_power_action,
                                       mock_get_task_monitor):
        """Verify consolidated reboot sets correct async flags."""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_COMPLETED
        mock_task.task_status = sushy.HEALTH_OK
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            setattr(task.node, step_attr,
                    {'step': 'update', 'interface': 'firmware'})
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_current_index', 1)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.set_driver_internal_info(
                'agent_secret_token', 'test-token')
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            info = task.node.driver_internal_info
            self.assertTrue(info.get(polling_flag))
            self.assertTrue(info.get(reboot_flag))
            self.assertNotIn('agent_secret_token', info)
            mock_power_action.assert_called_once_with(
                task, states.REBOOT, mock.ANY)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_batch_reboot_sets_flags_cleaning(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_power_action):
        self._test_batch_reboot_async_flags(
            'clean_step', async_steps.CLEANING_REBOOT,
            async_steps.CLEANING_POLLING, mock_power_action,
            mock_get_task_monitor)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_batch_reboot_sets_flags_servicing(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_power_action):
        self._test_batch_reboot_async_flags(
            'service_step', async_steps.SERVICING_REBOOT,
            async_steps.SERVICING_POLLING, mock_power_action,
            mock_get_task_monitor)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_batch_reboot_sets_flags_deploying(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_power_action):
        self._test_batch_reboot_async_flags(
            'deploy_step', async_steps.DEPLOYMENT_REBOOT,
            async_steps.DEPLOYMENT_POLLING, mock_power_action,
            mock_get_task_monitor)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_batch_reboot_preserves_pregenerated_token(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_power_action):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_COMPLETED
        mock_task.task_status = sushy.HEALTH_OK
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'firmware_batch_current_index', 1)
            task.node.set_driver_internal_info('firmware_batched_update', True)
            task.node.set_driver_internal_info(
                'firmware_allow_grouping', True)
            task.node.set_driver_internal_info(
                'agent_secret_token', 'pregen-token')
            task.node.set_driver_internal_info(
                'agent_secret_token_pregenerated', True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            info = task.node.driver_internal_info
            self.assertEqual('pregen-token', info.get('agent_secret_token'))
            self.assertTrue(info.get('agent_secret_token_pregenerated'))

    def _test_bmc_completion_reboot_flags(self, step_attr, reboot_flag,
                                          polling_flag,
                                          mock_power_action,
                                          mock_get_bmc_version,
                                          mock_get_update_service):
        """Verify BMC completion reboot sets correct async flags."""
        settings = [
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0',
             'wait': 300, 'task_monitor': '/tasks/1',
             'bmc_check_start_time': '2025-01-01T00:00:00.000000'},
            {'component': 'nic:BCM57414', 'url': 'http://nic/v1.0.0',
             'task_monitor': '/tasks/2'}
        ]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            setattr(task.node, step_attr,
                    {'step': 'update', 'interface': 'firmware'})
            task.node.set_driver_internal_info(
                'bmc_fw_version_before_update', '1.0.0')
            task.node.set_driver_internal_info(
                'redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'agent_secret_token', 'test-token')
            task.node.save()

            mock_get_bmc_version.return_value = '2.0.0'

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._handle_bmc_update_completion(
                task, mock_get_update_service.return_value,
                settings, settings[0])

            info = task.node.driver_internal_info
            self.assertTrue(info.get(polling_flag))
            self.assertTrue(info.get(reboot_flag))
            self.assertNotIn('agent_secret_token', info)
            mock_power_action.assert_called_once_with(task, states.REBOOT)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_get_current_bmc_version', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_bmc_completion_reboot_sets_flags_cleaning(
            self, mock_get_update_service, mock_get_bmc_version,
            mock_power_action):
        self._test_bmc_completion_reboot_flags(
            'clean_step', async_steps.CLEANING_REBOOT,
            async_steps.CLEANING_POLLING,
            mock_power_action, mock_get_bmc_version,
            mock_get_update_service)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_get_current_bmc_version', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_bmc_completion_reboot_sets_flags_servicing(
            self, mock_get_update_service, mock_get_bmc_version,
            mock_power_action):
        self._test_bmc_completion_reboot_flags(
            'service_step', async_steps.SERVICING_REBOOT,
            async_steps.SERVICING_POLLING,
            mock_power_action, mock_get_bmc_version,
            mock_get_update_service)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_get_current_bmc_version', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_bmc_completion_reboot_sets_flags_deploying(
            self, mock_get_update_service, mock_get_bmc_version,
            mock_power_action):
        self._test_bmc_completion_reboot_flags(
            'deploy_step', async_steps.DEPLOYMENT_REBOOT,
            async_steps.DEPLOYMENT_POLLING,
            mock_power_action, mock_get_bmc_version,
            mock_get_update_service)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test_report_step_error_no_step_type(self, mock_log):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._report_step_error(
                task, 'test error message')

            mock_log.error.assert_called_once()
            call_args = mock_log.error.call_args[0]
            self.assertIn('No step type', call_args[0])

    @mock.patch.object(manager_utils, 'servicing_error_handler',
                       autospec=True)
    @mock.patch.object(manager_utils, 'deploying_error_handler',
                       autospec=True)
    def test_report_step_error_service_precedence(
            self, mock_deploy_error, mock_service_error):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.deploy_step = {'step': 'update',
                                     'interface': 'firmware'}

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._report_step_error(
                task, 'test error message')

            mock_service_error.assert_called_once_with(
                task, 'test error message', traceback=True)
            mock_deploy_error.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    def test_continue_updates_last_calls_cache(
            self, mock_resume_clean, mock_validate, mock_cache):
        self._generate_new_driver_internal_info(['bmc'])
        task = self._test_continue_updates()

        mock_cache.assert_called_once()
        mock_resume_clean.assert_called_once_with(task)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    def test_continue_updates_last_cache_exception_swallowed(
            self, mock_resume_clean, mock_validate, mock_cache):
        mock_cache.side_effect = Exception('cache failed')
        self._generate_new_driver_internal_info(['bmc'])
        task = self._test_continue_updates()

        mock_cache.assert_called_once()
        mock_resume_clean.assert_called_once_with(task)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_deploy',
                       autospec=True)
    def test_continue_updates_last_deploy(
            self, mock_resume_deploy, mock_validate, mock_cache):
        settings = [{'component': 'bios', 'url': 'http://bios/v1.0.0',
                     'task_monitor': '/tasks/1'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.deploy_step = {'step': 'update',
                                     'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._continue_updates(
                task, mock.Mock(), settings)

            mock_cache.assert_called_once()
            mock_resume_deploy.assert_called_once_with(task)

    # --- M-6 guard tests ---

    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_start_time_stamped_once_across_segments(
            self, mock_get_update_service, mock_get_system,
            mock_get_manager, mock_submit_simple_update):
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0'},
                    {'component': 'bios', 'url': 'http://bios/v1.0.0'}]
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.driver.firmware.update(task, settings)

            original_stamp = task.node.driver_internal_info.get(
                'redfish_fw_update_start_time')
            self.assertIsNotNone(original_stamp)

            remaining = [{'component': 'bios',
                          'url': 'http://bios/v1.0.0'}]
            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._start_next_segment(
                task, mock_get_update_service.return_value, remaining)

            after_stamp = task.node.driver_internal_info.get(
                'redfish_fw_update_start_time')
            self.assertEqual(original_stamp, after_stamp)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_batch_reboot_polling_true_even_when_ramdisk_disabled(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_power_action):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_COMPLETED
        mock_task.task_status = sushy.HEALTH_OK
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.clean_step = {'step': 'update',
                                    'interface': 'firmware'}
            task.node.provision_state = states.CLEANWAIT
            task.node.set_driver_internal_info(
                'cleaning_disable_ramdisk', True)
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCH_CURRENT_INDEX, 1)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCHED_UPDATE, True)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_ALLOW_GROUPING, True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            info = task.node.driver_internal_info
            self.assertTrue(info.get(async_steps.CLEANING_POLLING))
            self.assertTrue(info.get(async_steps.CLEANING_REBOOT))

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_batch_reboot_polling_true_when_ramdisk_active(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_power_action):
        # provision_state CLEANWAIT with NO cleaning_disable_ramdisk:
        # is_ramdisk_disabled() returns False here, so a reintroduced
        # polling=is_ramdisk_disabled(node) bug would set polling=False
        # and the assertion below would catch it.
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_COMPLETED
        mock_task.task_status = sushy.HEALTH_OK
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.clean_step = {'step': 'update',
                                    'interface': 'firmware'}
            task.node.provision_state = states.CLEANWAIT
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCH_CURRENT_INDEX, 1)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCHED_UPDATE, True)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_ALLOW_GROUPING, True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            info = task.node.driver_internal_info
            self.assertTrue(info.get(async_steps.CLEANING_POLLING))
            self.assertTrue(info.get(async_steps.CLEANING_REBOOT))

    # --- W-1 staged-pending disclosure tests ---

    @mock.patch.object(manager_utils, 'servicing_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_submit_one_batched_component', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_staged_pending_note_on_submission_failure(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_submit, mock_clear_updates, mock_error_handler):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
            {'component': 'nic:NIC.Slot.2', 'url': 'http://nic2/v2.0.0'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_COMPLETED
        mock_task.task_status = sushy.HEALTH_OK
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor
        mock_submit.side_effect = Exception('SimpleUpdate failed')

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCH_CURRENT_INDEX, 1)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCHED_UPDATE, True)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_ALLOW_GROUPING, True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_error_handler.assert_called_once()
            error_msg = mock_error_handler.call_args[0][1]
            self.assertIn('staged on the BMC', error_msg)
            self.assertIn('bios', error_msg)
            self.assertIn('nic:NIC.1-1', error_msg)

    @mock.patch.object(manager_utils, 'servicing_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_staged_pending_note_on_staging_task_failure(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_clear_updates, mock_error_handler):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
            {'component': 'nic:NIC.Slot.2', 'url': 'http://nic2/v2.0.0',
             'task_monitor': '/tasks/3'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_EXCEPTION
        mock_task.task_status = sushy.HEALTH_CRITICAL
        mock_msg = mock.Mock()
        mock_msg.message = 'Staging failed'
        mock_msg.message_id = 'MSG001'
        mock_task.messages = [mock_msg]
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCH_CURRENT_INDEX, 2)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCHED_UPDATE, True)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_ALLOW_GROUPING, True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_error_handler.assert_called_once()
            error_msg = mock_error_handler.call_args[0][1]
            self.assertIn('staged on the BMC', error_msg)
            self.assertIn('bios', error_msg)
            self.assertIn('nic:NIC.1-1', error_msg)

    @mock.patch.object(redfish_fw.manager_utils, 'cleaning_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_staged_pending_note_on_overall_timeout(
            self, mock_get_update_service, mock_error_handler):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        past_time = (timeutils.utcnow()
                     - datetime.timedelta(hours=3)).isoformat()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.clean_step = {'step': 'update',
                                    'interface': 'firmware'}
            task.node.provision_state = states.CLEANING
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                'redfish_fw_update_start_time', past_time)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCHED_UPDATE, True)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_ALLOW_GROUPING, True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_node_redfish_firmware_update(task)

            mock_error_handler.assert_called_once()
            error_msg = mock_error_handler.call_args[0][1]
            self.assertIn('timeout', error_msg.lower())
            self.assertIn('bios', error_msg)
            self.assertIn('nic:NIC.1-1', error_msg)

    @mock.patch.object(manager_utils, 'servicing_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_staged_pending_note_absent_in_phase2(
            self, mock_get_update_service, mock_get_task_monitor,
            mock_clear_updates, mock_error_handler):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0',
             'task_monitor': '/tasks/1'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0',
             'task_monitor': '/tasks/2'},
        ]

        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_EXCEPTION
        mock_task.task_status = sushy.HEALTH_CRITICAL
        mock_msg = mock.Mock()
        mock_msg.message = 'Apply failed'
        mock_msg.message_id = 'MSG001'
        mock_task.messages = [mock_msg]
        mock_monitor = mock.Mock()
        mock_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_monitor

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            task.node.set_driver_internal_info('redfish_fw_updates', settings)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCH_SUBMITTED, True)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_BATCHED_UPDATE, True)
            task.node.set_driver_internal_info(
                redfish_fw.FIRMWARE_ALLOW_GROUPING, True)
            task.node.save()

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._check_batched_update_status(task, settings)

            mock_error_handler.assert_called_once()
            error_msg = mock_error_handler.call_args[0][1]
            self.assertNotIn('staged on the BMC', error_msg)

    @mock.patch.object(manager_utils, 'servicing_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_submit_one_batched_component', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_staged_pending_note_absent_on_first_component_failure(
            self, mock_get_update_service, mock_submit,
            mock_clear_updates, mock_error_handler):
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0'},
            {'component': 'nic:NIC.1-1', 'url': 'http://nic/v2.0.0'},
            {'component': 'nic:NIC.Slot.2', 'url': 'http://nic2/v2.0.0'},
        ]
        mock_submit.side_effect = sushy.exceptions.SushyError(
            message='SimpleUpdate failed')

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            firmware_interface = redfish_fw.RedfishFirmware()
            self.assertRaises(
                sushy.exceptions.SushyError,
                firmware_interface._execute_batched_non_bmc_updates,
                task, mock_get_update_service.return_value, settings)
