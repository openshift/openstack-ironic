# Copyright 2017 Red Hat, Inc.
# All Rights Reserved.
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

import asyncio
import contextlib
import datetime
from unittest import mock

from oslo_utils import timeutils
from oslo_utils import units
import sushy
from sushy.resources.chassis.thermal import constants as sushy_thermal_const
from sushy.resources import constants as sushy_constants

from ironic.common import boot_devices
from ironic.common import boot_modes
from ironic.common import components
from ironic.common import exception
from ironic.common import health_states
from ironic.common import indicator_states
from ironic.common.redfish import exceptions as async_exceptions
from ironic.common.redfish import resource as async_resource
from ironic.common import states
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.conf import CONF
from ironic.drivers.modules import boot_mode_utils
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules.redfish import boot as redfish_boot
from ironic.drivers.modules.redfish import firmware_utils
from ironic.drivers.modules.redfish import management as redfish_mgmt
from ironic.drivers.modules.redfish import power as redfish_power
from ironic.drivers.modules.redfish import utils as redfish_utils
from ironic.tests.unit.db import base as db_base
from ironic.tests.unit.db import utils as db_utils
from ironic.tests.unit.objects import utils as obj_utils

INFO_DICT = db_utils.get_test_redfish_info()


class RedfishManagementTestCase(db_base.DbTestCase):

    def setUp(self):
        super(RedfishManagementTestCase, self).setUp()
        self.config(enabled_hardware_types=['redfish'],
                    enabled_power_interfaces=['redfish'],
                    enabled_boot_interfaces=['redfish-virtual-media'],
                    enabled_management_interfaces=['redfish'],
                    enabled_inspect_interfaces=['redfish'],
                    enabled_bios_interfaces=['redfish'])
        self.node = obj_utils.create_test_node(
            self.context, driver='redfish', driver_info=INFO_DICT)

        self.system_uuid = 'ZZZ--XXX-YYY'
        self.chassis_uuid = 'XXX-YYY-ZZZ'

    def init_system_mock(self, system_mock, **properties):

        system_mock.reset()

        system_mock.boot.mode = 'uefi'

        system_mock.memory_summary.size_gib = 2

        system_mock.processors.summary = '8', 'MIPS'

        system_mock.simple_storage.disks_sizes_bytes = (
            1 * units.Gi, units.Gi * 3, units.Gi * 5)
        system_mock.storage.volumes_sizes_bytes = (
            2 * units.Gi, units.Gi * 4, units.Gi * 6)

        system_mock.ethernet_interfaces.summary = {
            '00:11:22:33:44:55': sushy.STATE_ENABLED,
            '66:77:88:99:AA:BB': sushy.STATE_DISABLED,
        }

        return system_mock

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
            task.driver.management.validate(task)
            mock_parse_driver_info.assert_called_once_with(task.node)

    def test_get_supported_boot_devices(self):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            supported_boot_devices = (
                task.driver.management.get_supported_boot_devices(task))
            self.assertEqual(list(redfish_mgmt.BOOT_DEVICE_MAP_REV),
                             supported_boot_devices)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device(self, mock_get_system):
        fake_system = mock.Mock()
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            expected_values = [
                (boot_devices.PXE, sushy.BOOT_SOURCE_TARGET_PXE),
                (boot_devices.DISK, sushy.BOOT_SOURCE_TARGET_HDD),
                (boot_devices.CDROM, sushy.BOOT_SOURCE_TARGET_CD),
                (boot_devices.BIOS, sushy.BOOT_SOURCE_TARGET_BIOS_SETUP),
                (boot_devices.UEFIHTTP, sushy.BOOT_SOURCE_TARGET_UEFI_HTTP)
            ]

            for target, expected in expected_values:
                task.driver.management.set_boot_device(task, target)

                # Asserts
                fake_system.set_system_boot_options.assert_has_calls(
                    [mock.call(expected,
                               enabled=sushy.BOOT_SOURCE_ENABLED_ONCE,
                               http_boot_uri=None)])
                mock_get_system.assert_called_with(task.node)
                self.assertNotIn('redfish_boot_device',
                                 task.node.driver_internal_info)

                # Reset mocks
                fake_system.set_system_boot_options.reset_mock()
                mock_get_system.reset_mock()

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_persistency(self, mock_get_system):
        fake_system = mock.Mock()
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            expected_values = [
                (True, sushy.BOOT_SOURCE_ENABLED_CONTINUOUS),
                (False, sushy.BOOT_SOURCE_ENABLED_ONCE)
            ]

            for target, expected in expected_values:
                task.driver.management.set_boot_device(
                    task, boot_devices.PXE, persistent=target)

                fake_system.set_system_boot_options.assert_has_calls(
                    [mock.call(sushy.BOOT_SOURCE_TARGET_PXE,
                               enabled=expected, http_boot_uri=None)])
                mock_get_system.assert_called_with(task.node)
                self.assertNotIn('redfish_boot_device',
                                 task.node.driver_internal_info)

                # Reset mocks
                fake_system.set_system_boot_options.reset_mock()
                mock_get_system.reset_mock()

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_persistency_no_change(self, mock_get_system):
        fake_system = mock.Mock()
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            expected_values = [
                (True, sushy.BOOT_SOURCE_ENABLED_CONTINUOUS),
                (False, sushy.BOOT_SOURCE_ENABLED_ONCE)
            ]

            for target, expected in expected_values:
                fake_system.boot.get.return_value = expected

                task.driver.management.set_boot_device(
                    task, boot_devices.PXE, persistent=target)

                fake_system.set_system_boot_options.assert_has_calls(
                    [mock.call(sushy.BOOT_SOURCE_TARGET_PXE, enabled=None,
                               http_boot_uri=None)])
                mock_get_system.assert_called_with(task.node)

                # Reset mocks
                fake_system.set_system_boot_options.reset_mock()
                mock_get_system.reset_mock()

    @mock.patch.object(sushy, 'Sushy', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_fail(self, mock_get_system, mock_sushy):
        fake_system = mock.Mock()
        fake_system.set_system_boot_options.side_effect = (
            sushy.exceptions.SushyError()
        )
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError, 'Redfish set boot device',
                task.driver.management.set_boot_device, task, boot_devices.PXE)
            fake_system.set_system_boot_options.assert_called_once_with(
                sushy.BOOT_SOURCE_TARGET_PXE,
                enabled=sushy.BOOT_SOURCE_ENABLED_ONCE,
                http_boot_uri=None)
            mock_get_system.assert_called_once_with(task.node)
            self.assertNotIn('redfish_boot_device',
                             task.node.driver_internal_info)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_fail_no_change(self, mock_get_system):
        fake_system = mock.Mock()
        fake_system.set_system_boot_options.side_effect = (
            sushy.exceptions.SushyError()
        )
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            expected_values = [
                (True, sushy.BOOT_SOURCE_ENABLED_CONTINUOUS),
                (False, sushy.BOOT_SOURCE_ENABLED_ONCE)
            ]

            for target, expected in expected_values:
                fake_system.boot.get.return_value = expected

                self.assertRaisesRegex(
                    exception.RedfishError, 'Redfish set boot device',
                    task.driver.management.set_boot_device, task,
                    boot_devices.PXE, persistent=target)
                fake_system.set_system_boot_options.assert_called_once_with(
                    sushy.BOOT_SOURCE_TARGET_PXE, enabled=None,
                    http_boot_uri=None)
                mock_get_system.assert_called_once_with(task.node)
                self.assertNotIn('redfish_boot_device',
                                 task.node.driver_internal_info)

                # Reset mocks
                fake_system.set_system_boot_options.reset_mock()
                mock_get_system.reset_mock()

    @mock.patch.object(sushy, 'Sushy', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_persistence_fallback(self, mock_get_system,
                                                  mock_sushy):
        fake_system = mock.Mock()
        fake_system.set_system_boot_options.side_effect = [
            sushy.exceptions.SushyError(),
            None,
            None
        ]
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_boot_device(
                task, boot_devices.PXE, persistent=True)
            fake_system.set_system_boot_options.assert_has_calls([
                mock.call(sushy.BOOT_SOURCE_TARGET_PXE,
                          enabled=sushy.BOOT_SOURCE_ENABLED_CONTINUOUS,
                          http_boot_uri=None),
                mock.call(sushy.BOOT_SOURCE_TARGET_PXE,
                          enabled=sushy.BOOT_SOURCE_ENABLED_ONCE,
                          http_boot_uri=None)
            ])
            mock_get_system.assert_called_with(task.node)

            task.node.refresh()
            self.assertEqual(
                boot_devices.PXE,
                task.node.driver_internal_info['redfish_boot_device'])

    @mock.patch.object(boot_mode_utils, 'sync_boot_mode', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_persistency_vendor(self, mock_get_system,
                                                mock_sync_boot_mode):
        fake_system = mock_get_system.return_value
        fake_system.boot.get.return_value = \
            sushy.BOOT_SOURCE_ENABLED_CONTINUOUS

        values = [
            ('SuperMicro', sushy.BOOT_SOURCE_ENABLED_CONTINUOUS),
            ('SomeVendor', None)
        ]

        for vendor, expected in values:
            properties = self.node.properties
            properties['vendor'] = vendor
            self.node.properties = properties
            self.node.save()
            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=False) as task:
                task.driver.management.set_boot_device(
                    task, boot_devices.PXE, persistent=True)
                fake_system.set_system_boot_options.assert_called_once_with(
                    sushy.BOOT_SOURCE_TARGET_PXE, enabled=expected,
                    http_boot_uri=None)
                if vendor == 'SuperMicro':
                    mock_sync_boot_mode.assert_called_once_with(task)
                else:
                    mock_sync_boot_mode.assert_not_called()

                # Reset mocks
                fake_system.set_system_boot_options.reset_mock()
                mock_sync_boot_mode.reset_mock()
                mock_get_system.reset_mock()

    @mock.patch.object(boot_mode_utils, 'sync_boot_mode', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_full_boot_request_vendor(self, mock_get_system,
                                                      mock_sync_boot_mode):
        """Vendors in VENDORS_REQUIRING_FULL_BOOT_REQUEST always send enabled.

        Unlike the default path which skips enabled when it matches the
        current value, full-boot-request vendors must always send enabled
        along with mode.
        """
        fake_system = mock_get_system.return_value
        # Set current enabled to CONTINUOUS — same as desired (persistent=True)
        fake_system.boot.get.side_effect = lambda key, *a: {
            'enabled': sushy.BOOT_SOURCE_ENABLED_CONTINUOUS,
            'mode': sushy.BOOT_SOURCE_MODE_UEFI,
        }.get(key)

        properties = self.node.properties
        properties['vendor'] = 'American Megatrends International'
        self.node.properties = properties
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_boot_device(
                task, boot_devices.PXE, persistent=True)
            # enabled must be CONTINUOUS even though current is already
            # CONTINUOUS (the default path would pass None here).
            fake_system.set_system_boot_options.assert_called_once_with(
                sushy.BOOT_SOURCE_TARGET_PXE,
                mode=sushy.BOOT_SOURCE_MODE_UEFI,
                enabled=sushy.BOOT_SOURCE_ENABLED_CONTINUOUS,
                http_boot_uri=None)
            mock_sync_boot_mode.assert_not_called()

    @mock.patch.object(sushy, 'Sushy', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_persistence_fallback_full_boot_vendor(
            self, mock_get_system, mock_sushy):
        """CONTINUOUS fallback triggers for full-boot-request vendors.

        Before the fix, full-boot-request vendors used a different variable
        (desired_enabled) for the call than the one checked in the fallback
        (enabled), so the fallback would never trigger. Regression test for
        LP#2142498.
        """
        fake_system = mock.Mock()
        fake_system.boot.get.side_effect = lambda key, *a: {
            'enabled': sushy.BOOT_SOURCE_ENABLED_CONTINUOUS,
            'mode': sushy.BOOT_SOURCE_MODE_UEFI,
        }.get(key)
        fake_system.set_system_boot_options.side_effect = [
            sushy.exceptions.SushyError(),
            None,
        ]
        mock_get_system.return_value = fake_system

        properties = self.node.properties
        properties['vendor'] = 'AMI'
        self.node.properties = properties
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_boot_device(
                task, boot_devices.PXE, persistent=True)
            fake_system.set_system_boot_options.assert_has_calls([
                mock.call(sushy.BOOT_SOURCE_TARGET_PXE,
                          mode=sushy.BOOT_SOURCE_MODE_UEFI,
                          enabled=sushy.BOOT_SOURCE_ENABLED_CONTINUOUS,
                          http_boot_uri=None),
                mock.call(sushy.BOOT_SOURCE_TARGET_PXE,
                          enabled=sushy.BOOT_SOURCE_ENABLED_ONCE,
                          http_boot_uri=None)
            ])

            task.node.refresh()
            self.assertEqual(
                boot_devices.PXE,
                task.node.driver_internal_info['redfish_boot_device'])

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_http_boot(self, mock_get_system):
        fake_system = mock.Mock()
        mock_get_system.return_value = fake_system
        self.node.driver_internal_info = {
            'redfish_uefi_http_url': 'http://foo.url'}
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_boot_device(task,
                                                   boot_devices.UEFIHTTP)
            fake_system.set_system_boot_options.assert_has_calls(
                [mock.call(sushy.BOOT_SOURCE_TARGET_UEFI_HTTP,
                           enabled=sushy.BOOT_SOURCE_ENABLED_ONCE,
                           http_boot_uri='http://foo.url')])
            mock_get_system.assert_called_with(task.node)
            self.assertNotIn('redfish_boot_device',
                             task.node.driver_internal_info)
            task.node.refresh()
            self.assertNotIn('redfish_uefi_http_url',
                             task.node.driver_internal_info)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_post_retry_success(self, mock_get_system):
        """Test successful retry when BMC rejects changes during POST."""
        # Configure minimal retries for faster test
        self.config(post_boot_retry_attempts=3, group='redfish')
        self.config(post_boot_retry_delay=1, group='redfish')

        fake_system = mock.Mock()
        # First call fails with POST error, second succeeds
        # Create a real BadRequestError with POST error message in str()
        mock_response = mock.Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            'error': {'message': 'UnableToModifyDuringSystemPOST'}
        }
        post_error = sushy.exceptions.BadRequestError(
            'POST', 'http://bmc/redfish', mock_response)
        fake_system.set_system_boot_options.side_effect = [
            post_error,
            None  # Success on retry
        ]
        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_boot_device(task, boot_devices.PXE)

            # Should have been called twice (initial + 1 retry)
            self.assertEqual(2,
                             fake_system.set_system_boot_options.call_count)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_post_retry_exhausted(self, mock_get_system):
        """Test that POST retry eventually gives up and raises."""
        # Configure minimal retries for faster test
        self.config(post_boot_retry_attempts=2, group='redfish')
        self.config(post_boot_retry_delay=1, group='redfish')

        fake_system = mock.Mock()
        # All calls fail with POST error
        # Create a real BadRequestError with POST error message in str()
        mock_response = mock.Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            'error': {'message': 'UnableToModifyDuringSystemPOST'}
        }
        post_error = sushy.exceptions.BadRequestError(
            'POST', 'http://bmc/redfish', mock_response)
        fake_system.set_system_boot_options.side_effect = post_error
        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError, 'Redfish set boot device',
                task.driver.management.set_boot_device, task, boot_devices.PXE)

            # Should have been called for all retry attempts
            self.assertEqual(2,
                             fake_system.set_system_boot_options.call_count)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_bmc_retry_success(self, mock_get_system):
        """Test successful retry when BMC asks that a retry is attempted."""
        # Configure minimal retries for faster test
        self.config(post_boot_retry_attempts=3, group='redfish')
        self.config(post_boot_retry_delay=1, group='redfish')

        fake_system = mock.Mock()
        mock_response = mock.Mock()
        mock_response.status_code = 503
        mock_response.json.return_value = {
            'error': {
                'message': 'The service is temporarily unavailable.  Retry '
                           'in 30 seconds.'
            }
        }
        retry_error = sushy.exceptions.ServerSideError(
            'PATCH', 'http://bmc/redfish', mock_response)
        fake_system.set_system_boot_options.side_effect = [
            retry_error,
            None  # Success on retry
        ]
        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_boot_device(task, boot_devices.PXE)

            # Should have been called twice (initial + 1 retry)
            self.assertEqual(2,
                             fake_system.set_system_boot_options.call_count)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_bmc_retry_exhausted(self, mock_get_system):
        """Test if the BMC always asks it eventually gives up and raises."""
        # Configure minimal retries for faster test
        self.config(post_boot_retry_attempts=2, group='redfish')
        self.config(post_boot_retry_delay=1, group='redfish')

        fake_system = mock.Mock()
        mock_response = mock.Mock()
        mock_response.status_code = 503
        mock_response.json.return_value = {
            'error': {
                'message': 'The service is temporarily unavailable.  Retry '
                           'in 30 seconds.'
            }
        }
        retry_error = sushy.exceptions.ServerSideError(
            'PATCH', 'http://bmc/redfish', mock_response)
        fake_system.set_system_boot_options.side_effect = retry_error
        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError, 'Redfish set boot device',
                task.driver.management.set_boot_device, task, boot_devices.PXE)

            # Should have been called for all retry attempts
            self.assertEqual(2,
                             fake_system.set_system_boot_options.call_count)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_device_other_error_no_retry(self, mock_get_system):
        """Test that other errors are not retried."""
        # Configure retries (should not be used)
        self.config(post_boot_retry_attempts=3, group='redfish')
        self.config(post_boot_retry_delay=1, group='redfish')

        fake_system = mock.Mock()
        fake_system.set_system_boot_options.side_effect = (
            sushy.exceptions.SushyError('Some other error')
        )
        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError, 'Redfish set boot device',
                task.driver.management.set_boot_device, task, boot_devices.PXE)

            # Should have been called only once
            # (no retries if there is no POST error and no retry asked for)
            fake_system.set_system_boot_options.assert_called_once()

    def test_restore_boot_device(self):
        fake_system = mock.Mock()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.driver_internal_info['redfish_boot_device'] = (
                boot_devices.DISK
            )

            task.driver.management.restore_boot_device(task, fake_system)

            fake_system.set_system_boot_options.assert_called_once_with(
                sushy.BOOT_SOURCE_TARGET_HDD,
                enabled=sushy.BOOT_SOURCE_ENABLED_ONCE,
                http_boot_uri=None)
            # The stored boot device is kept intact
            self.assertEqual(
                boot_devices.DISK,
                task.node.driver_internal_info['redfish_boot_device'])

    def test_restore_boot_device_compat(self):
        fake_system = mock.Mock()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            # Previously we used sushy constants
            task.node.driver_internal_info['redfish_boot_device'] = "hdd"

            task.driver.management.restore_boot_device(task, fake_system)

            fake_system.set_system_boot_options.assert_called_once_with(
                sushy.BOOT_SOURCE_TARGET_HDD,
                enabled=sushy.BOOT_SOURCE_ENABLED_ONCE,
                http_boot_uri=None)
            # The stored boot device is kept intact
            self.assertEqual(
                "hdd",
                task.node.driver_internal_info['redfish_boot_device'])

    def test_restore_boot_device_noop(self):
        fake_system = mock.Mock()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.restore_boot_device(task, fake_system)

            self.assertFalse(fake_system.set_system_boot_options.called)

    @mock.patch.object(redfish_mgmt.LOG, 'warning', autospec=True)
    def test_restore_boot_device_failure(self, mock_log):
        fake_system = mock.Mock()
        fake_system.set_system_boot_options.side_effect = (
            sushy.exceptions.SushyError()
        )
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.driver_internal_info['redfish_boot_device'] = (
                boot_devices.DISK
            )

            task.driver.management.restore_boot_device(task, fake_system)

            fake_system.set_system_boot_options.assert_called_once_with(
                sushy.BOOT_SOURCE_TARGET_HDD,
                enabled=sushy.BOOT_SOURCE_ENABLED_ONCE,
                http_boot_uri=None)
            self.assertTrue(mock_log.called)
            # The stored boot device is kept intact
            self.assertEqual(
                boot_devices.DISK,
                task.node.driver_internal_info['redfish_boot_device'])

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_boot_device(self, mock_get_system):
        boot_attribute = {
            'target': sushy.BOOT_SOURCE_TARGET_PXE,
            'enabled': sushy.BOOT_SOURCE_ENABLED_CONTINUOUS
        }
        fake_system = mock.Mock(boot=boot_attribute)
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            response = task.driver.management.get_boot_device(task)
            expected = {'boot_device': boot_devices.PXE,
                        'persistent': True}
            self.assertEqual(expected, response)

    def test_get_supported_boot_modes(self):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            supported_boot_modes = (
                task.driver.management.get_supported_boot_modes(task))
            self.assertEqual(list(redfish_mgmt.BOOT_MODE_MAP_REV),
                             supported_boot_modes)

    @mock.patch.object(redfish_mgmt.RedfishManagement, '_wait_for_boot_mode',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_mode(self, mock_get_system, mock_wait):
        boot_attribute = {
            'target': sushy.BOOT_SOURCE_TARGET_PXE,
            'enabled': sushy.BOOT_SOURCE_ENABLED_CONTINUOUS,
            'mode': None,
        }
        fake_system = mock.Mock(boot=boot_attribute)
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            expected_values = [
                (boot_modes.LEGACY_BIOS, sushy.BOOT_SOURCE_MODE_BIOS,
                 sushy.BOOT_SOURCE_MODE_UEFI),
                (boot_modes.UEFI, sushy.BOOT_SOURCE_MODE_UEFI,
                 sushy.BOOT_SOURCE_MODE_BIOS),
                (boot_modes.LEGACY_BIOS, sushy.BOOT_SOURCE_MODE_BIOS, None),
                (boot_modes.UEFI, sushy.BOOT_SOURCE_MODE_UEFI, None),
            ]

            for mode, expected, current in expected_values:
                boot_attribute['mode'] = current
                task.driver.management.set_boot_mode(task, mode=mode)

                # Asserts
                fake_system.set_system_boot_options.assert_called_once_with(
                    mode=expected)
                mock_get_system.assert_called_once_with(task.node)
                if current is not None:
                    mock_wait.assert_called_once_with(task.driver.management,
                                                      task, fake_system, mode)
                else:
                    mock_wait.assert_not_called()

                # Reset mocks
                fake_system.set_system_boot_options.reset_mock()
                mock_get_system.reset_mock()
                mock_wait.reset_mock()

    @mock.patch.object(sushy, 'Sushy', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_mode_fail(self, mock_get_system, mock_sushy):
        boot_attribute = {
            'target': sushy.BOOT_SOURCE_TARGET_PXE,
            'enabled': sushy.BOOT_SOURCE_ENABLED_CONTINUOUS,
            'mode': sushy.BOOT_SOURCE_MODE_BIOS,
        }
        fake_system = mock.Mock(boot=boot_attribute)
        fake_system.set_system_boot_options.side_effect = (
            sushy.exceptions.SushyError)
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError, 'Setting boot mode',
                task.driver.management.set_boot_mode, task, boot_modes.UEFI)
            fake_system.set_system_boot_options.assert_called_once_with(
                mode=sushy.BOOT_SOURCE_MODE_UEFI)
            mock_get_system.assert_called_once_with(task.node)

    @mock.patch.object(sushy, 'Sushy', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_boot_mode_unsupported(self, mock_get_system, mock_sushy):
        boot_attribute = {
            'target': sushy.BOOT_SOURCE_TARGET_PXE,
            'enabled': sushy.BOOT_SOURCE_ENABLED_CONTINUOUS,
        }
        fake_system = mock.Mock(boot=boot_attribute)
        error = sushy.exceptions.BadRequestError('PATCH', '/', mock.Mock())
        fake_system.set_system_boot_options.side_effect = error
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.UnsupportedDriverExtension,
                'does not support set_boot_mode',
                task.driver.management.set_boot_mode, task, boot_modes.UEFI)
            fake_system.set_system_boot_options.assert_called_once_with(
                mode=sushy.BOOT_SOURCE_MODE_UEFI)
            mock_get_system.assert_called_once_with(task.node)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    def test_wait_for_boot_mode_immediate(self, mock_power):
        fake_system = mock.Mock(spec=['boot', 'refresh'],
                                boot={'mode': sushy.BOOT_SOURCE_MODE_UEFI})
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management._wait_for_boot_mode(
                task, fake_system, boot_modes.UEFI)
            fake_system.refresh.assert_called_once_with(force=True)
            mock_power.assert_not_called()

    @mock.patch('time.sleep', lambda _: None)
    @mock.patch.object(redfish_power.RedfishPower, 'get_power_state',
                       autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    def test_wait_for_boot_mode(self, mock_power, mock_get_power):
        attempts = 3

        def side_effect(force):
            nonlocal attempts
            attempts -= 1
            if attempts <= 0:
                fake_system.boot['mode'] = sushy.BOOT_SOURCE_MODE_UEFI

        fake_system = mock.Mock(spec=['boot', 'refresh'],
                                boot={'mode': sushy.BOOT_SOURCE_MODE_BIOS})
        fake_system.refresh.side_effect = side_effect
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management._wait_for_boot_mode(
                task, fake_system, boot_modes.UEFI)
            fake_system.refresh.assert_called_with(force=True)
            self.assertEqual(3, fake_system.refresh.call_count)
            mock_power.assert_has_calls([
                mock.call(task, states.REBOOT),
                mock.call(task, mock_get_power.return_value),
            ])

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_boot_mode(self, mock_get_system):
        boot_attribute = {
            'target': sushy.BOOT_SOURCE_TARGET_PXE,
            'enabled': sushy.BOOT_SOURCE_ENABLED_CONTINUOUS,
            'mode': sushy.BOOT_SOURCE_MODE_BIOS,
        }
        fake_system = mock.Mock(boot=boot_attribute)
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            response = task.driver.management.get_boot_mode(task)
            expected = boot_modes.LEGACY_BIOS
            self.assertEqual(expected, response)

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_bmc_clock_success(self, mock_get_system, mock_get_manager):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            mock_system = mock.Mock()
            mock_manager = mock.Mock()
            mock_manager.datetime = '2025-06-27T12:00:00'

            mock_get_system.return_value = mock_system
            mock_get_manager.return_value = mock_manager

            target_datetime = '2025-06-27T12:00:00'
            task.driver.management.set_bmc_clock(
                task, target_datetime=target_datetime)

            mock_manager.set_datetime.assert_called_once_with(
                target_datetime, None)
            mock_manager.refresh.assert_called_once()

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_bmc_clock_defaults_to_conductor_time(self, mock_get_system,
                                                      mock_get_manager):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            current_datetime = datetime.datetime(
                2025, 6, 27, 12, 0, 0, 123456)
            expected_datetime = current_datetime.replace(
                tzinfo=datetime.timezone.utc,
                microsecond=0).isoformat()

            mock_system = mock.Mock()
            mock_manager = mock.Mock()
            mock_manager.datetime = '2025-06-27T12:00:00+00:00'

            mock_get_system.return_value = mock_system
            mock_get_manager.return_value = mock_manager

            with mock.patch.object(redfish_mgmt.timeutils, 'utcnow',
                                   autospec=True,
                                   return_value=current_datetime):
                task.driver.management.set_bmc_clock(task)

            mock_manager.set_datetime.assert_called_once_with(
                expected_datetime, None)
            mock_manager.refresh.assert_called_once()

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_bmc_clock_datetime_mismatch_raises(self, mock_get_system,
                                                    mock_get_manager):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            mock_system = mock.Mock()
            mock_manager = mock.Mock()
            mock_manager.datetime = '2025-01-01T00:00:00'

            mock_get_system.return_value = mock_system
            mock_get_manager.return_value = mock_manager

            with self.assertRaisesRegex(
                    exception.RedfishError,
                    'BMC clock update failed: mismatch after setting '
                    'datetime'):
                task.driver.management.set_bmc_clock(
                    task, target_datetime='2025-06-27T12:00:00')

            mock_manager.set_datetime.assert_called_once()
            mock_manager.refresh.assert_called_once()

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(CONF.redfish, 'enable_verify_bmc_clock', new=False)
    def test_verify_bmc_clock_disabled_in_config(self, mock_get_system,
                                                 mock_get_manager):

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.verify_bmc_clock(task)

        # Ensure Redfish was never called
        mock_get_system.assert_not_called()
        mock_get_manager.assert_not_called()

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(timeutils, 'utcnow', autospec=True)
    def test_verify_bmc_clock_success_when_time_matches(self, mock_utcnow,
                                                        mock_get_system,
                                                        mock_get_manager):

        self.config(enable_verify_bmc_clock=True, group='redfish')
        current_datetime = datetime.datetime(2025, 7, 13, 3, 0, 0)
        mock_utcnow.return_value = current_datetime

        self.node.updated_at = datetime.datetime.now(
            datetime.timezone.utc).replace(tzinfo=None)
        self.node.save()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            mock_system = mock.Mock()
            mock_manager = mock.Mock()

            mock_manager.datetime = current_datetime.replace(
                tzinfo=datetime.timezone.utc).isoformat()
            mock_manager.datetimelocaloffset = '+00:00'

            mock_get_system.return_value = mock_system
            mock_get_manager.return_value = mock_manager

            task.driver.management.verify_bmc_clock(task)

            mock_manager.set_datetime.assert_not_called()
            mock_manager.refresh.assert_called_once()

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(timeutils, 'utcnow', autospec=True)
    def test_verify_bmc_clock_fails_on_mismatch(self, mock_utcnow,
                                                mock_get_system,
                                                mock_get_manager):
        self.config(enable_verify_bmc_clock=True, group='redfish')

        current_datetime = datetime.datetime(2025, 7, 13, 3, 0, 0)
        mock_utcnow.return_value = current_datetime

        self.node.updated_at = datetime.datetime.now(
            datetime.timezone.utc).replace(tzinfo=None)
        self.node.save()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:

            if task.node is None:
                task.node = self.node
            if not getattr(task.node, 'uuid', None):
                task.node.uuid = self.node.uuid

            mock_system = mock.Mock()
            mock_manager = mock.Mock()

            # Mismatched time (e.g., 5 seconds later)
            mock_manager.datetime = (
                current_datetime + datetime.timedelta(seconds=5)).replace(
                    tzinfo=datetime.timezone.utc).isoformat()
            mock_manager.datetimelocaloffset = "+00:00"

            mock_get_system.return_value = mock_system
            mock_get_manager.return_value = mock_manager

            management = redfish_mgmt.RedfishManagement()

        with self.assertRaisesRegex(exception.NodeVerifyFailure,
                                    "BMC clock verify step failed"):
            management.verify_bmc_clock(task)

        self.assertEqual(mock_manager.refresh.call_count, 2)
        mock_manager.set_datetime.assert_called_once_with(
            current_datetime.replace(
                tzinfo=datetime.timezone.utc).isoformat(),
            datetime_local_offset=None)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_inject_nmi(self, mock_get_system):
        fake_system = mock.Mock()
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.inject_nmi(task)
            fake_system.reset_system.assert_called_once_with(sushy.RESET_NMI)
            mock_get_system.assert_called_once_with(task.node)

    @mock.patch.object(sushy, 'Sushy', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_inject_nmi_fail(self, mock_get_system, mock_sushy):
        fake_system = mock.Mock()
        fake_system.reset_system.side_effect = (
            sushy.exceptions.SushyError)
        mock_get_system.return_value = fake_system
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError, 'Redfish inject NMI',
                task.driver.management.inject_nmi, task)
            fake_system.reset_system.assert_called_once_with(
                sushy.RESET_NMI)
            mock_get_system.assert_called_once_with(task.node)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_supported_indicators(self, mock_get_system):
        fake_chassis = mock.Mock(
            uuid=self.chassis_uuid,
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_drive = mock.Mock(
            identity='drive1',
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_storage = mock.Mock(
            identity='storage1',
            drives=[fake_drive])
        fake_system = mock.Mock(
            uuid=self.system_uuid,
            chassis=[fake_chassis],
            storage=mock.MagicMock(),
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_system.storage.get_members.return_value = [fake_storage]

        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:

            supported_indicators = (
                task.driver.management.get_supported_indicators(task))

            expected = {
                components.CHASSIS: {
                    'XXX-YYY-ZZZ': {
                        "readonly": False,
                        "states": [
                            indicator_states.BLINKING,
                            indicator_states.OFF,
                            indicator_states.ON
                        ]
                    }
                },
                components.SYSTEM: {
                    'ZZZ--XXX-YYY': {
                        "readonly": False,
                        "states": [
                            indicator_states.BLINKING,
                            indicator_states.OFF,
                            indicator_states.ON
                        ]
                    }
                },
                components.DISK: {
                    'storage1:drive1': {
                        "readonly": False,
                        "states": [
                            indicator_states.BLINKING,
                            indicator_states.OFF,
                            indicator_states.ON
                        ]
                    }
                }
            }

            self.assertEqual(expected, supported_indicators)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_indicator_state(self, mock_get_system):
        fake_chassis = mock.Mock(
            uuid=self.chassis_uuid,
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_drive = mock.Mock(
            identity='drive1',
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_storage = mock.Mock(
            identity='storage1',
            drives=[fake_drive])
        fake_system = mock.Mock(
            uuid=self.system_uuid,
            chassis=[fake_chassis],
            storage=mock.MagicMock(),
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_system.storage.get_members.return_value = [fake_storage]

        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_indicator_state(
                task, components.SYSTEM, self.system_uuid, indicator_states.ON)

            fake_system.set_indicator_led.assert_called_once_with(
                sushy.INDICATOR_LED_LIT)

            mock_get_system.assert_called_once_with(task.node)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_indicator_state_disk(self, mock_get_system):
        fake_chassis = mock.Mock(
            uuid=self.chassis_uuid,
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_drive = mock.Mock(
            identity='drive1',
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_storage = mock.Mock(
            identity='storage1',
            drives=[fake_drive])
        fake_system = mock.Mock(
            uuid=self.system_uuid,
            chassis=[fake_chassis],
            storage=mock.MagicMock(),
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_system.storage.get_members.return_value = [fake_storage]

        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_indicator_state(
                task, components.DISK, 'storage1:drive1', indicator_states.ON)

            fake_drive.set_indicator_led.assert_called_once_with(
                sushy.INDICATOR_LED_LIT)

            mock_get_system.assert_called_once_with(task.node)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_indicator_state(self, mock_get_system):
        fake_chassis = mock.Mock(
            uuid=self.chassis_uuid,
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_drive = mock.Mock(
            identity='drive1',
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_storage = mock.Mock(
            identity='storage1',
            drives=[fake_drive])
        fake_system = mock.Mock(
            uuid=self.system_uuid,
            chassis=[fake_chassis],
            storage=mock.MagicMock(),
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_system.storage.get_members.return_value = [fake_storage]

        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:

            state = task.driver.management.get_indicator_state(
                task, components.SYSTEM, self.system_uuid)

            mock_get_system.assert_called_once_with(task.node)

            self.assertEqual(indicator_states.ON, state)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_indicator_state_disk(self, mock_get_system):
        fake_chassis = mock.Mock(
            uuid=self.chassis_uuid,
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_drive = mock.Mock(
            identity='drive1',
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_storage = mock.Mock(
            identity='storage1',
            drives=[fake_drive])
        fake_system = mock.Mock(
            uuid=self.system_uuid,
            chassis=[fake_chassis],
            storage=mock.MagicMock(),
            indicator_led=sushy.INDICATOR_LED_LIT)
        fake_system.storage.get_members.return_value = [fake_storage]

        mock_get_system.return_value = fake_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:

            state = task.driver.management.get_indicator_state(
                task, components.DISK, 'storage1:drive1')

            mock_get_system.assert_called_once_with(task.node)

            self.assertEqual(indicator_states.ON, state)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_root_vendor', autospec=True)
    def test_detect_vendor(self, mock_get_root_vendor, mock_get_system):
        mock_get_root_vendor.return_value = "AMI"
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            response = task.driver.management.detect_vendor(task)
            self.assertEqual("AMI", response)
            mock_get_system.assert_not_called()

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_root_vendor', autospec=True)
    def test_detect_vendor_no_root_vendor(self, mock_get_root_vendor,
                                          mock_get_system):
        mock_get_root_vendor.return_value = None
        mock_get_system.return_value.manufacturer = "Fake GmbH"
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            response = task.driver.management.detect_vendor(task)
            self.assertEqual("Fake GmbH", response)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_root_vendor', autospec=True)
    def test_detect_vendor_root_vendor_empty(self, mock_get_root_vendor,
                                             mock_get_system):
        mock_get_root_vendor.return_value = ""
        mock_get_system.return_value.manufacturer = "Fake GmbH"
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            response = task.driver.management.detect_vendor(task)
            self.assertEqual("Fake GmbH", response)

    @mock.patch.object(deploy_utils, 'build_agent_options',
                       spec_set=True, autospec=True)
    @mock.patch.object(redfish_boot.RedfishVirtualMediaBoot, 'prepare_ramdisk',
                       spec_set=True, autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_firmware(self, mock_get_update_service,
                             mock_set_async_step_flags,
                             mock_node_power_action, mock_prepare,
                             build_mock):
        build_mock.return_value = {'a': 'b'}
        mock_task_monitor = mock.Mock()
        mock_task_monitor.task_monitor_uri = '/task/123'
        mock_update_service = mock.Mock()
        mock_update_service.simple_update.return_value = mock_task_monitor
        mock_get_update_service.return_value = mock_update_service
        CONF.set_override('firmware_source', 'http', 'redfish')
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.save = mock.Mock()
            task.node.deploy_step = {
                'step': 'update_firmware',
                'interface': 'management'}

            result = task.driver.management.update_firmware(
                task,
                [{'url': 'http://test1',
                  'checksum': 'aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d'},
                 {'url': 'http://test2',
                  'checksum': '9f6227549221920e312fed2cfc6586ee832cc546'}])
            self.assertEqual(states.DEPLOYWAIT, result)

            mock_get_update_service.assert_called_once_with(task.node)
            mock_update_service.simple_update.assert_called_once_with(
                'http://test1')
            self.assertIsNotNone(task.node
                                 .driver_internal_info['firmware_updates'])
            self.assertEqual(
                [{'task_monitor': '/task/123', 'url': 'http://test1',
                  'checksum': 'aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d'},
                 {'url': 'http://test2',
                  'checksum': '9f6227549221920e312fed2cfc6586ee832cc546'}],
                task.node.driver_internal_info['firmware_updates'])
            self.assertIsNone(
                task.node.driver_internal_info.get('firmware_cleanup'))
            mock_set_async_step_flags.assert_called_once_with(
                task.node, reboot=True, skip_current_step=True, polling=True)
            mock_node_power_action.assert_called_once_with(
                task, states.REBOOT)

    @mock.patch.object(redfish_mgmt.RedfishManagement, '_stage_firmware_file',
                       autospec=True)
    @mock.patch.object(deploy_utils, 'build_agent_options',
                       spec_set=True, autospec=True)
    @mock.patch.object(redfish_boot.RedfishVirtualMediaBoot, 'prepare_ramdisk',
                       spec_set=True, autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_firmware_stage(
            self, mock_get_update_service, mock_set_async_step_flags,
            mock_node_power_action, mock_prepare, build_mock, mock_stage):
        build_mock.return_value = {'a': 'b'}
        mock_task_monitor = mock.Mock()
        mock_task_monitor.task_monitor_uri = '/task/123'
        mock_update_service = mock.Mock()
        mock_update_service.simple_update.return_value = mock_task_monitor
        mock_get_update_service.return_value = mock_update_service
        mock_stage.return_value = ('http://staged/test1', 'http')
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.save = mock.Mock()
            task.node.deploy_step = {
                'step': 'update_firmware',
                'interface': 'management'}

            task.driver.management.update_firmware(
                task,
                [{'url': 'http://test1',
                  'checksum': 'aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d'},
                 {'url': 'http://test2',
                  'checksum': '9f6227549221920e312fed2cfc6586ee832cc546'}])

            mock_get_update_service.assert_called_once_with(task.node)
            mock_update_service.simple_update.assert_called_once_with(
                'http://staged/test1')
            self.assertIsNotNone(task.node
                                 .driver_internal_info['firmware_updates'])
            self.assertEqual(
                [{'task_monitor': '/task/123', 'url': 'http://test1',
                  'checksum': 'aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d'},
                 {'url': 'http://test2',
                  'checksum': '9f6227549221920e312fed2cfc6586ee832cc546'}],
                task.node.driver_internal_info['firmware_updates'])
            self.assertIsNotNone(
                task.node.driver_internal_info['firmware_cleanup'])
            self.assertEqual(
                ['http'], task.node.driver_internal_info['firmware_cleanup'])
            mock_set_async_step_flags.assert_called_once_with(
                task.node, reboot=True, skip_current_step=True, polling=True)
            mock_node_power_action.assert_called_once_with(
                task, states.REBOOT)

    @mock.patch.object(redfish_mgmt.RedfishManagement, '_stage_firmware_file',
                       autospec=True)
    @mock.patch.object(deploy_utils, 'build_agent_options',
                       spec_set=True, autospec=True)
    @mock.patch.object(redfish_boot.RedfishVirtualMediaBoot, 'prepare_ramdisk',
                       spec_set=True, autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_firmware_stage_both(
            self, mock_get_update_service, mock_set_async_step_flags,
            mock_node_power_action, mock_prepare, build_mock, mock_stage):
        build_mock.return_value = {'a': 'b'}
        mock_task_monitor = mock.Mock()
        mock_task_monitor.task_monitor_uri = '/task/123'
        mock_update_service = mock.Mock()
        mock_update_service.simple_update.return_value = mock_task_monitor
        mock_get_update_service.return_value = mock_update_service
        mock_stage.return_value = ('http://staged/test1', 'http')
        info = self.node.driver_internal_info
        info['firmware_cleanup'] = ['swift']
        self.node.driver_internal_info = info
        self.node.deploy_step = {'step': 'update_firmware',
                                 'interface': 'management'}
        self.node.save()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.save = mock.Mock()

            task.driver.management.update_firmware(
                task,
                [{'url': 'http://test1',
                  'checksum': 'aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d'},
                 {'url': 'http://test2',
                  'checksum': '9f6227549221920e312fed2cfc6586ee832cc546'}])

            mock_get_update_service.assert_called_once_with(task.node)
            mock_update_service.simple_update.assert_called_once_with(
                'http://staged/test1')
            self.assertIsNotNone(task.node
                                 .driver_internal_info['firmware_updates'])
            self.assertEqual(
                [{'task_monitor': '/task/123', 'url': 'http://test1',
                  'checksum': 'aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d'},
                 {'url': 'http://test2',
                  'checksum': '9f6227549221920e312fed2cfc6586ee832cc546'}],
                task.node.driver_internal_info['firmware_updates'])
            self.assertIsNotNone(
                task.node.driver_internal_info['firmware_cleanup'])
            self.assertEqual(
                ['swift', 'http'],
                task.node.driver_internal_info['firmware_cleanup'])
            mock_set_async_step_flags.assert_called_once_with(
                task.node, reboot=True, skip_current_step=True, polling=True)
            mock_node_power_action.assert_called_once_with(
                task, states.REBOOT)

    def test_update_firmware_invalid_args(self):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaises(
                exception.InvalidParameterValue,
                task.driver.management.update_firmware,
                task, [{'urlX': 'test1'}, {'url': 'test2'}])

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def test__query_firmware_update_failed(self, mock_acquire):
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '', driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(management=management))
        mock_acquire.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))
        management._clear_firmware_updates = mock.Mock()

        management._query_firmware_update_failed(mock_manager,
                                                 self.context)

        management._clear_firmware_updates.assert_called_once_with(self.node)

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def test__query_firmware_update_failed_no_firmware_upd(self, mock_acquire):
        driver_internal_info = {'something': 'else'}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '', driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(management=management))
        mock_acquire.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))
        management._clear_firmware_updates = mock.Mock()

        management._query_firmware_update_failed(mock_manager,
                                                 self.context)

        management._clear_firmware_updates.assert_not_called()

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def test__query_firmware_update_status(self, mock_acquire):
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '', driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(management=management))
        mock_acquire.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))
        management._check_node_firmware_update = mock.Mock()

        management._query_firmware_update_status(mock_manager,
                                                 self.context)

        management._check_node_firmware_update.assert_called_once_with(task)

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def test__query_firmware_update_status_no_firmware_upd(self, mock_acquire):
        driver_internal_info = {'something': 'else'}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '', driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(management=management))
        mock_acquire.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))
        management._check_node_firmware_update = mock.Mock()

        management._query_firmware_update_status(mock_manager,
                                                 self.context)

        management._check_node_firmware_update.assert_not_called()

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def test__query_firmware_update_failed_deployfail(self, mock_acquire):
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.provision_state = states.DEPLOYFAIL
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '', driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(management=management))
        mock_acquire.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))
        management._clear_firmware_updates = mock.Mock()

        management._query_firmware_update_failed(mock_manager,
                                                 self.context)

        management._clear_firmware_updates.assert_called_once_with(self.node)

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def test__query_firmware_update_failed_servicefail(self, mock_acquire):
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.provision_state = states.SERVICEFAIL
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '', driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(management=management))
        mock_acquire.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))
        management._clear_firmware_updates = mock.Mock()

        management._query_firmware_update_failed(mock_manager,
                                                 self.context)

        management._clear_firmware_updates.assert_called_once_with(self.node)

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def test__query_firmware_update_status_deploywait(self, mock_acquire):
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.provision_state = states.DEPLOYWAIT
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '', driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(management=management))
        mock_acquire.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))
        management._check_node_firmware_update = mock.Mock()

        management._query_firmware_update_status(mock_manager,
                                                 self.context)

        management._check_node_firmware_update.assert_called_once_with(task)

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def test__query_firmware_update_status_servicewait(self, mock_acquire):
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.provision_state = states.SERVICEWAIT
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '', driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(management=management))
        mock_acquire.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))
        management._check_node_firmware_update = mock.Mock()

        management._query_firmware_update_status(mock_manager,
                                                 self.context)

        management._check_node_firmware_update.assert_called_once_with(task)

    @mock.patch.object(redfish_mgmt.LOG, 'warning', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test__check_node_firmware_update_redfish_conn_error(
            self, mock_get_update_services, mock_log):
        mock_get_update_services.side_effect = exception.RedfishConnectionError
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()

        management = redfish_mgmt.RedfishManagement()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._check_node_firmware_update(task)

        self.assertTrue(mock_log.called)

    @mock.patch.object(redfish_mgmt.LOG, 'debug', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test__check_node_firmware_update_wait_elapsed(
            self, mock_get_update_service, mock_log):
        mock_update_service = mock.Mock()
        mock_get_update_service.return_value = mock_update_service

        wait_start_time = timeutils.utcnow() - datetime.timedelta(minutes=15)
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1',
                 'wait_start_time':
                    wait_start_time.isoformat(),
                 'wait': 1}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        management._continue_firmware_updates = mock.Mock()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._check_node_firmware_update(task)

            self.assertTrue(mock_log.called)
            management._continue_firmware_updates.assert_called_once_with(
                task,
                mock_update_service,
                [{'task_monitor': '/task/123', 'url': 'test1'}])

    @mock.patch.object(redfish_mgmt.LOG, 'debug', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test__check_node_firmware_update_still_waiting(
            self, mock_get_update_service, mock_log):
        mock_update_service = mock.Mock()
        mock_get_update_service.return_value = mock_update_service

        wait_start_time = timeutils.utcnow() - datetime.timedelta(minutes=1)
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1',
                 'wait_start_time':
                     wait_start_time.isoformat(),
                 'wait': 600}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        management._continue_firmware_updates = mock.Mock()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._check_node_firmware_update(task)

            self.assertTrue(mock_log.called)
            management._continue_firmware_updates.assert_not_called()

    @mock.patch.object(redfish_mgmt.LOG, 'warning', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_task_monitor_not_found(
            self, mock_task_monitor, mock_get_update_service, mock_log):
        mock_task_monitor.side_effect = exception.RedfishError()
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        management._continue_firmware_updates = mock.Mock()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._check_node_firmware_update(task)

            self.assertTrue(mock_log.called)
            management._continue_firmware_updates.assert_called_once_with(
                task,
                mock_get_update_service.return_value,
                [{'task_monitor': '/task/123', 'url': 'test1'}])

    @mock.patch.object(redfish_mgmt.LOG, 'debug', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_in_progress(self,
                                                     mock_get_task_monitor,
                                                     mock_get_update_service,
                                                     mock_log):
        mock_get_task_monitor.return_value.is_processing = True
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._check_node_firmware_update(task)

            self.assertTrue(mock_log.called)

    @mock.patch.object(manager_utils, 'cleaning_error_handler', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_fail(self,
                                              mock_get_task_monitor,
                                              mock_get_update_service,
                                              mock_cleaning_error_handler):
        mock_sushy_task = mock.Mock()
        mock_sushy_task.task_state = 'exception'
        mock_message_unparsed = mock.Mock()
        mock_message_unparsed.message = None
        mock_message = mock.Mock()
        mock_message.message = 'Firmware upgrade failed'
        messages = mock.PropertyMock(side_effect=[[mock_message_unparsed],
                                                  [mock_message],
                                                  [mock_message]])
        type(mock_sushy_task).messages = messages
        mock_task_monitor = mock.Mock()
        mock_task_monitor.is_processing = False
        mock_task_monitor.get_task.return_value = mock_sushy_task
        mock_get_task_monitor.return_value = mock_task_monitor
        driver_internal_info = {'something': 'else',
                                'firmware_updates': [
                                    {'task_monitor': '/task/123',
                                     'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        management._continue_firmware_updates = mock.Mock()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.upgrade_lock = mock.Mock()
            task.process_event = mock.Mock()

            management._check_node_firmware_update(task)

            task.upgrade_lock.assert_called_once_with()
            self.assertEqual({'something': 'else'},
                             task.node.driver_internal_info)
            mock_cleaning_error_handler.assert_called_once()
            management._continue_firmware_updates.assert_not_called()

    @mock.patch.object(redfish_mgmt.LOG, 'info', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_done(self,
                                              mock_get_task_monitor,
                                              mock_get_update_service,
                                              mock_log):
        mock_task = mock.Mock()
        mock_task.task_state = sushy.TASK_STATE_COMPLETED
        mock_task.task_status = sushy.HEALTH_OK
        mock_message = mock.Mock()
        mock_message.message = 'Firmware update done'
        mock_task.messages = [mock_message]
        mock_task_monitor = mock.Mock()
        mock_task_monitor.is_processing = False
        mock_task_monitor.get_task.return_value = mock_task
        mock_get_task_monitor.return_value = mock_task_monitor
        driver_internal_info = {
            'firmware_updates': [
                {'task_monitor': '/task/123',
                 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.save()
        management = redfish_mgmt.RedfishManagement()
        management._continue_firmware_updates = mock.Mock()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._check_node_firmware_update(task)

            self.assertTrue(mock_log.called)
            management._continue_firmware_updates.assert_called_once_with(
                task,
                mock_get_update_service.return_value,
                [{'task_monitor': '/task/123',
                  'url': 'test1'}])

    @mock.patch.object(redfish_mgmt.LOG, 'debug', autospec=True)
    def test__continue_firmware_updates_wait(self, mock_log):
        mock_update_service = mock.Mock()

        management = redfish_mgmt.RedfishManagement()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._continue_firmware_updates(
                task,
                mock_update_service,
                [{'task_monitor': '/task/123',
                  'url': 'test1',
                  'wait': 10,
                  'wait_start_time': '20200901123045'},
                 {'url': 'test2'}])

            self.assertTrue(mock_log.called)
            # Wait start time has changed
            self.assertNotEqual(
                '20200901123045',
                task.node.driver_internal_info['firmware_updates']
                [0]['wait_start_time'])

    @mock.patch.object(redfish_mgmt.LOG, 'info', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    def test__continue_firmware_updates_last_update(
            self,
            mock_notify_conductor_resume_clean,
            mock_log):
        mock_update_service = mock.Mock()
        driver_internal_info = {
            'something': 'else',
            'firmware_updates': [
                {'task_monitor': '/task/123', 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.clean_step = {'interface': 'management',
                                'step': 'update_firmware'}
        self.node.save()

        management = redfish_mgmt.RedfishManagement()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._continue_firmware_updates(
                task,
                mock_update_service,
                [{'task_monitor': '/task/123', 'url': 'test1'}])

            self.assertTrue(mock_log.called)
            mock_notify_conductor_resume_clean.assert_called_once_with(task)
            self.assertEqual({'something': 'else'},
                             task.node.driver_internal_info)

    @mock.patch.object(redfish_mgmt.LOG, 'info', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_service',
                       autospec=True)
    def test__continue_firmware_updates_last_update_service(
            self,
            mock_notify_conductor_resume_service,
            mock_log):
        mock_update_service = mock.Mock()
        driver_internal_info = {
            'something': 'else',
            'firmware_updates': [
                {'task_monitor': '/task/123', 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.service_step = {'interface': 'management',
                                  'step': 'update_firmware'}
        self.node.save()

        management = redfish_mgmt.RedfishManagement()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._continue_firmware_updates(
                task,
                mock_update_service,
                [{'task_monitor': '/task/123', 'url': 'test1'}])

            self.assertTrue(mock_log.called)
            mock_notify_conductor_resume_service.assert_called_once_with(task)
            self.assertEqual({'something': 'else'},
                             task.node.driver_internal_info)

    @mock.patch.object(redfish_mgmt.LOG, 'info', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_deploy',
                       autospec=True)
    def test__continue_firmware_updates_last_update_deploy(
            self,
            mock_notify_conductor_resume_deploy,
            mock_log):
        mock_update_service = mock.Mock()
        driver_internal_info = {
            'something': 'else',
            'firmware_updates': [
                {'task_monitor': '/task/123', 'url': 'test1'}]}
        self.node.driver_internal_info = driver_internal_info
        self.node.deploy_step = {'interface': 'management',
                                 'step': 'update_firmware'}
        self.node.save()

        management = redfish_mgmt.RedfishManagement()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            management._continue_firmware_updates(
                task,
                mock_update_service,
                [{'task_monitor': '/task/123', 'url': 'test1'}])

            self.assertTrue(mock_log.called)
            mock_notify_conductor_resume_deploy.assert_called_once_with(task)
            self.assertEqual({'something': 'else'},
                             task.node.driver_internal_info)

    @mock.patch.object(redfish_mgmt.LOG, 'debug', autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    def test__continue_firmware_updates_more_updates(self,
                                                     mock_node_power_action,
                                                     mock_log):
        mock_task_monitor = mock.Mock()
        mock_task_monitor.task_monitor_uri = '/task/987'
        mock_update_service = mock.Mock()
        mock_update_service.simple_update.return_value = mock_task_monitor
        driver_internal_info = {
            'something': 'else',
            'firmware_updates': [
                {'task_monitor': '/task/123', 'url': 'http://test1'},
                {'url': 'http://test2'}]}
        self.node.driver_internal_info = driver_internal_info
        CONF.set_override('firmware_source', 'http', 'redfish')

        management = redfish_mgmt.RedfishManagement()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.save = mock.Mock()

            management._continue_firmware_updates(
                task,
                mock_update_service,
                [{'task_monitor': '/task/123', 'url': 'http://test1'},
                 {'url': 'http://test2'}])

            self.assertTrue(mock_log.called)
            mock_update_service.simple_update.assert_called_once_with(
                'http://test2')
            self.assertIsNotNone(
                task.node.driver_internal_info['firmware_updates'])
            self.assertEqual(
                [{'url': 'http://test2', 'task_monitor': '/task/987'}],
                task.node.driver_internal_info['firmware_updates'])
            task.node.save.assert_called_once_with()
            mock_node_power_action.assert_called_once_with(task, states.REBOOT)

    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    @mock.patch.object(firmware_utils, 'verify_checksum', autospec=True)
    @mock.patch.object(firmware_utils, 'stage', autospec=True)
    def test__stage_firmware_file_https(self, mock_stage, mock_verify_checksum,
                                        mock_download_to_temp):
        CONF.set_override('firmware_source', 'local', 'redfish')
        firmware_update = {'url': 'https://test1', 'checksum': 'abc'}
        node = mock.Mock()
        mock_download_to_temp.return_value = '/tmp/test1'
        mock_stage.return_value = ('http://staged/test1', 'http')

        management = redfish_mgmt.RedfishManagement()

        staged_url, needs_cleanup = management._stage_firmware_file(
            node, firmware_update)

        self.assertEqual(staged_url, 'http://staged/test1')
        self.assertEqual(needs_cleanup, 'http')
        mock_download_to_temp.assert_called_with(node, 'https://test1')
        mock_verify_checksum.assert_called_with(node, 'abc', '/tmp/test1')
        mock_stage.assert_called_with(node, 'local', '/tmp/test1')

    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    @mock.patch.object(firmware_utils, 'verify_checksum', autospec=True)
    @mock.patch.object(firmware_utils, 'stage', autospec=True)
    @mock.patch.object(firmware_utils, 'get_swift_temp_url', autospec=True)
    def test__stage_firmware_file_swift(
            self, mock_get_swift_temp_url, mock_stage, mock_verify_checksum,
            mock_download_to_temp):
        CONF.set_override('firmware_source', 'swift', 'redfish')
        firmware_update = {'url': 'swift://container/bios.exe'}
        node = mock.Mock()
        mock_get_swift_temp_url.return_value = 'http://temp'

        management = redfish_mgmt.RedfishManagement()

        staged_url, needs_cleanup = management._stage_firmware_file(
            node, firmware_update)

        self.assertEqual(staged_url, 'http://temp')
        self.assertIsNone(needs_cleanup)
        mock_download_to_temp.assert_not_called()
        mock_verify_checksum.assert_not_called()
        mock_stage.assert_not_called()

    @mock.patch.object(firmware_utils, 'cleanup', autospec=True)
    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    @mock.patch.object(firmware_utils, 'verify_checksum', autospec=True)
    @mock.patch.object(firmware_utils, 'stage', autospec=True)
    def test__stage_firmware_file_error(self, mock_stage, mock_verify_checksum,
                                        mock_download_to_temp, mock_cleanup):
        node = mock.Mock()
        firmware_update = {'url': 'https://test1'}
        CONF.set_override('firmware_source', 'local', 'redfish')
        firmware_update = {'url': 'https://test1'}
        node = mock.Mock()
        mock_download_to_temp.return_value = '/tmp/test1'
        mock_stage.side_effect = exception.IronicException

        management = redfish_mgmt.RedfishManagement()
        self.assertRaises(exception.IronicException,
                          management._stage_firmware_file, node,
                          firmware_update)
        mock_download_to_temp.assert_called_with(node, 'https://test1')
        mock_verify_checksum.assert_called_with(node, None, '/tmp/test1')
        mock_stage.assert_called_with(node, 'local', '/tmp/test1')
        mock_cleanup.assert_called_with(node)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_secure_boot_state(self, mock_get_system):
        fake_system = mock_get_system.return_value
        fake_system.secure_boot.enabled = False
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            response = task.driver.management.get_secure_boot_state(task)
            self.assertIs(False, response)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_secure_boot_state_not_implemented(self, mock_get_system):
        # Yes, seriously, that's the only way to do it.
        class NoSecureBoot(mock.Mock):
            @property
            def secure_boot(self):
                raise sushy.exceptions.MissingAttributeError("boom")

        mock_get_system.return_value = NoSecureBoot()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertRaises(exception.UnsupportedDriverExtension,
                              task.driver.management.get_secure_boot_state,
                              task)

    @mock.patch.object(redfish_mgmt.RedfishManagement, '_wait_for_secure_boot',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_secure_boot_state(self, mock_get_system, mock_wait):
        fake_system = mock_get_system.return_value
        fake_system.secure_boot.enabled = False
        fake_system.boot = {'mode': sushy.BOOT_SOURCE_MODE_UEFI}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_secure_boot_state(task, True)
            fake_system.secure_boot.set_enabled.assert_called_once_with(True)
            mock_wait.assert_called_once_with(task.driver.management,
                                              task, fake_system.secure_boot,
                                              True)

    @mock.patch.object(redfish_mgmt.RedfishManagement, '_wait_for_secure_boot',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_secure_boot_state_boot_mode_unknown(self, mock_get_system,
                                                     mock_wait):
        fake_system = mock_get_system.return_value
        fake_system.secure_boot.enabled = False
        fake_system.boot = {}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_secure_boot_state(task, True)
            fake_system.secure_boot.set_enabled.assert_called_once_with(True)
            mock_wait.assert_called_once_with(task.driver.management,
                                              task, fake_system.secure_boot,
                                              True)

    @mock.patch.object(redfish_mgmt.RedfishManagement, '_wait_for_secure_boot',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_secure_boot_state_boot_mode_no_change(self, mock_get_system,
                                                       mock_wait):
        fake_system = mock_get_system.return_value
        fake_system.secure_boot.enabled = False
        fake_system.boot = {'mode': sushy.BOOT_SOURCE_MODE_BIOS}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management.set_secure_boot_state(task, False)
            fake_system.secure_boot.set_enabled.assert_not_called()
            mock_wait.assert_not_called()

    @mock.patch.object(redfish_mgmt.RedfishManagement, '_wait_for_secure_boot',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_secure_boot_state_boot_mode_incorrect(self, mock_get_system,
                                                       mock_wait):
        fake_system = mock_get_system.return_value
        fake_system.secure_boot.enabled = False
        fake_system.boot = {'mode': sushy.BOOT_SOURCE_MODE_BIOS}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError, 'requires UEFI',
                task.driver.management.set_secure_boot_state, task, True)
            fake_system.secure_boot.set_enabled.assert_not_called()
            mock_wait.assert_not_called()

    @mock.patch.object(redfish_mgmt.RedfishManagement, '_wait_for_secure_boot',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_secure_boot_state_boot_mode_fails(self, mock_get_system,
                                                   mock_wait):
        fake_system = mock_get_system.return_value
        fake_system.secure_boot.enabled = False
        fake_system.secure_boot.set_enabled.side_effect = \
            sushy.exceptions.SushyError
        fake_system.boot = {'mode': sushy.BOOT_SOURCE_MODE_UEFI}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError, 'Failed to set secure boot',
                task.driver.management.set_secure_boot_state, task, True)
            fake_system.secure_boot.set_enabled.assert_called_once_with(True)
            mock_wait.assert_not_called()

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_set_secure_boot_state_not_implemented(self, mock_get_system):
        # Yes, seriously, that's the only way to do it.
        class NoSecureBoot(mock.Mock):
            @property
            def secure_boot(self):
                raise sushy.exceptions.MissingAttributeError("boom")

        mock_get_system.return_value = NoSecureBoot()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaises(exception.UnsupportedDriverExtension,
                              task.driver.management.set_secure_boot_state,
                              task, True)

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    def test_wait_for_secure_boot_immediate(self, mock_power):
        fake_sb = mock.Mock(spec=['enabled', 'refresh'], enabled=True)
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management._wait_for_secure_boot(task, fake_sb, True)
            fake_sb.refresh.assert_called_once_with(force=True)
            mock_power.assert_not_called()

    @mock.patch('time.sleep', lambda _: None)
    @mock.patch.object(redfish_power.RedfishPower, 'get_power_state',
                       autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    def test_wait_for_secure_boot(self, mock_power, mock_get_power):
        attempts = 3

        def side_effect(force):
            nonlocal attempts
            attempts -= 1
            if attempts >= 2:
                raise sushy.exceptions.ServerSideError(
                    "POST", 'img-url', mock.MagicMock())
            if attempts <= 0:
                fake_sb.enabled = True

        fake_sb = mock.Mock(spec=['enabled', 'refresh'], enabled=False)
        fake_sb.refresh.side_effect = side_effect
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management._wait_for_secure_boot(task, fake_sb, True)
            fake_sb.refresh.assert_called_with(force=True)
            self.assertEqual(3, fake_sb.refresh.call_count)
            mock_power.assert_has_calls([
                mock.call(task, states.REBOOT),
                mock.call(task, mock_get_power.return_value),
            ])

    @mock.patch.object(redfish_mgmt, 'BOOT_MODE_CONFIG_INTERVAL', 0.1)
    @mock.patch.object(redfish_power.RedfishPower, 'get_power_state',
                       autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    def test_wait_for_secure_boot_timeout(self, mock_power, mock_get_power):
        CONF.set_override('boot_mode_config_timeout', 1, group='redfish')
        fake_sb = mock.Mock(spec=['enabled', 'refresh'], enabled=False)
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError, 'Timeout reached',
                task.driver.management._wait_for_secure_boot,
                task, fake_sb, True)
            fake_sb.refresh.assert_called_with(force=True)
            mock_power.assert_called_once_with(task, states.REBOOT)

    @mock.patch.object(redfish_power.RedfishPower, 'get_power_state',
                       autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    def test_wait_for_secure_boot_no_wait(self, mock_power, mock_get_power):
        CONF.set_override('boot_mode_config_timeout', 0, group='redfish')
        fake_sb = mock.Mock(spec=['enabled', 'refresh'], enabled=False)
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.management._wait_for_secure_boot(task, fake_sb, True)
            fake_sb.refresh.assert_called_once_with(force=True)
            mock_power.assert_has_calls([
                mock.call(task, states.REBOOT),
                mock.call(task, mock_get_power.return_value),
            ])

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_reset_secure_boot_to_default(self, mock_get_system):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.management.reset_secure_boot_keys_to_default(task)
            sb = mock_get_system.return_value.secure_boot
            sb.reset_keys.assert_called_once_with(
                sushy.SECURE_BOOT_RESET_KEYS_TO_DEFAULT)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_reset_secure_boot_to_default_not_implemented(self,
                                                          mock_get_system):
        class NoSecureBoot(mock.Mock):
            @property
            def secure_boot(self):
                raise sushy.exceptions.MissingAttributeError("boom")

        mock_get_system.return_value = NoSecureBoot()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertRaises(
                exception.UnsupportedDriverExtension,
                task.driver.management.reset_secure_boot_keys_to_default, task)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_clear_secure_boot(self, mock_get_system):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.management.clear_secure_boot_keys(task)
            sb = mock_get_system.return_value.secure_boot
            sb.reset_keys.assert_called_once_with(
                sushy.SECURE_BOOT_RESET_KEYS_DELETE_ALL)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_clear_secure_boot_not_implemented(self, mock_get_system):
        class NoSecureBoot(mock.Mock):
            @property
            def secure_boot(self):
                raise sushy.exceptions.MissingAttributeError("boom")

        mock_get_system.return_value = NoSecureBoot()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertRaises(
                exception.UnsupportedDriverExtension,
                task.driver.management.clear_secure_boot_keys, task)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_mac_addresses_success(self, mock_get_system):
        self.init_system_mock(mock_get_system.return_value)

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertEqual(['00:11:22:33:44:55'],
                             task.driver.management.get_mac_addresses(task))

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_mac_addresses_no_ports_found(self, mock_get_system):

        system_mock = self.init_system_mock(mock_get_system.return_value)
        system_mock.ethernet_interfaces.summary = None
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertEqual([],
                             task.driver.management.get_mac_addresses(task))

    @mock.patch.object(redfish_utils, 'get_enabled_macs', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_mac_addresses_missing_attr(self, mock_get_system,
                                            mock_get_enabled_macs):
        redfish_utils.get_enabled_macs.side_effect = (sushy.exceptions.
                                                      MissingAttributeError)
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertIsNone(task.driver.management.get_mac_addresses(task))

    @mock.patch.object(redfish_boot, 'get_vmedia', autospec=True)
    def test_get_virtual_media(self, mock_get_vmedia):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.management.get_virtual_media(task)
            mock_get_vmedia.assert_called_once_with(task)

    @mock.patch.object(redfish_boot, 'insert_vmedia', autospec=True)
    def test_attach_virtual_media(self, mock_insert_vmedia):
        with task_manager.acquire(self.context, self.node.uuid) as task:
            task.driver.management.attach_virtual_media(task, 'cdrom',
                                                        'http://test.iso')
            mock_insert_vmedia.assert_called_once_with(task, 'http://test.iso',
                                                       sushy.VIRTUAL_MEDIA_CD)

    @mock.patch.object(redfish_boot, 'eject_vmedia', autospec=True)
    def test_detach_virtual_media(self, mock_eject_vmedia):
        with task_manager.acquire(self.context, self.node.uuid) as task:
            task.driver.management.detach_virtual_media(task, ['cdrom'])
            mock_eject_vmedia.assert_called_once_with(task,
                                                      sushy.VIRTUAL_MEDIA_CD)

    @mock.patch.object(redfish_boot, 'eject_vmedia', autospec=True)
    def test_detach_virtual_media_all(self, mock_eject_vmedia):
        with task_manager.acquire(self.context, self.node.uuid) as task:
            task.driver.management.detach_virtual_media(task)
            mock_eject_vmedia.assert_called_once_with(task)


class SensorDataTestCase(db_base.DbTestCase):

    def setUp(self):
        super(SensorDataTestCase, self).setUp()
        self.config(enabled_hardware_types=['redfish'],
                    enabled_power_interfaces=['redfish'],
                    enabled_boot_interfaces=['redfish-virtual-media'],
                    enabled_management_interfaces=['redfish'],
                    enabled_inspect_interfaces=['redfish'],
                    enabled_bios_interfaces=['redfish'])
        self.node = obj_utils.create_test_node(
            self.context, driver='redfish', driver_info=INFO_DICT)

        self.system_uuid = 'ZZZ--XXX-YYY'
        self.chassis_uuid = 'XXX-YYY-ZZZ'

    def test__get_sensors_fan(self):
        attributes = {
            "identity": "XXX-YYY-ZZZ",
            "name": "CPU Fan",
            "status": {
                "state": "enabled",
                "health": "OK"
            },
            "reading": 6000,
            "reading_units": "RPM",
            "lower_threshold_fatal": 2000,
            "min_reading_range": 0,
            "max_reading_range": 10000,
            "serial_number": "SN010203040506",
            "physical_context": "CPU"
        }

        mock_chassis = mock.MagicMock(identity='ZZZ-YYY-XXX')

        mock_fan = mock.MagicMock(**attributes)
        mock_fan.name = attributes['name']
        mock_fan.status = mock.MagicMock(**attributes['status'])
        mock_chassis.thermal.fans = [mock_fan]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            sensors = task.driver.management._get_sensors_fan(mock_chassis)

        expected = {
            'XXX-YYY-ZZZ@ZZZ-YYY-XXX': {
                'identity': 'XXX-YYY-ZZZ',
                'max_reading_range': 10000,
                'min_reading_range': 0,
                'physical_context': 'CPU',
                'reading': 6000,
                'reading_units': 'RPM',
                'serial_number': 'SN010203040506',
                'health': 'OK',
                'state': 'enabled'
            }
        }

        self.assertEqual(expected, sensors)

    def test__get_sensors_temperatures(self):
        attributes = {
            "identity": "XXX-YYY-ZZZ",
            "name": "CPU Temp",
            "status": {
                "state": "enabled",
                "health": "OK"
            },
            "reading_celsius": 62,
            "upper_threshold_non_critical": 75,
            "upper_threshold_critical": 90,
            "upperThresholdFatal": 95,
            "min_reading_range_temp": 0,
            "max_reading_range_temp": 120,
            "physical_context": "CPU",
            "sensor_number": 1
        }

        mock_chassis = mock.MagicMock(identity='ZZZ-YYY-XXX')

        mock_temperature = mock.MagicMock(**attributes)
        mock_temperature.name = attributes['name']
        mock_temperature.status = mock.MagicMock(**attributes['status'])
        mock_chassis.thermal.temperatures = [mock_temperature]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            sensors = task.driver.management._get_sensors_temperatures(
                mock_chassis)

        expected = {
            'XXX-YYY-ZZZ@ZZZ-YYY-XXX': {
                'identity': 'XXX-YYY-ZZZ',
                'max_reading_range_temp': 120,
                'min_reading_range_temp': 0,
                'physical_context': 'CPU',
                'reading_celsius': 62,
                'sensor_number': 1,
                'health': 'OK',
                'state': 'enabled'
            }
        }

        self.assertEqual(expected, sensors)

    def test__get_sensors_power(self):
        attributes = {
            'identity': 0,
            'name': 'Power Supply 0',
            'power_capacity_watts': 1450,
            'last_power_output_watts': 650,
            'line_input_voltage': 220,
            'serial_number': 'SN010203040506',
            "status": {
                "state": "enabled",
                "health": "OK"
            }
        }

        mock_chassis = mock.MagicMock(identity='ZZZ-YYY-XXX')
        mock_power = mock_chassis.power
        mock_power.identity = 'Power'
        mock_psu = mock.MagicMock(**attributes)
        mock_psu.name = attributes['name']
        mock_psu.status = mock.MagicMock(**attributes['status'])
        mock_power.power_supplies = [mock_psu]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            sensors = task.driver.management._get_sensors_power(mock_chassis)

        expected = {
            '0:Power@ZZZ-YYY-XXX': {
                'health': 'OK',
                'last_power_output_watts': 650,
                'line_input_voltage': 220,
                'power_capacity_watts': 1450,
                'serial_number': 'SN010203040506',
                'state': 'enabled'
            }
        }

        self.assertEqual(expected, sensors)

    def test__get_sensor_drive_simple_storage(self):
        attributes = {
            'name': '32ADF365C6C1B7BD',
            'manufacturer': 'IBM',
            'model': 'IBM 350A',
            'capacity_bytes': 3750000000,
            'status': {
                'health': 'OK',
                'state': 'enabled'
            }
        }

        system_identity = 'ZZZ-YYY-XXX'
        mock_device = mock.MagicMock(**attributes)
        mock_device.name = attributes['name']
        mock_device.status = mock.MagicMock(**attributes['status'])
        mock_simple_storage = mock.MagicMock(
            spec=sushy.resources.system.simple_storage.SimpleStorage)
        mock_simple_storage.devices = [mock_device]
        mock_simple_storage.identity = 'XXX-YYY-ZZZ'

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            # Test individual device processing for SimpleStorage
            unique_name, sensor = task.driver.management._get_sensor_drive(
                mock_device, mock_simple_storage.identity, system_identity)

        expected_name = '32ADF365C6C1B7BD:XXX-YYY-ZZZ@ZZZ-YYY-XXX'
        expected_sensor = {
            'capacity_bytes': 3750000000,
            'health': 'OK',
            'name': '32ADF365C6C1B7BD',
            'model': 'IBM 350A',
            'state': 'enabled'
        }

        self.assertEqual(expected_name, unique_name)
        self.assertEqual(expected_sensor, sensor)

    def test__get_sensor_drive_storage(self):
        attributes = {
            'name': '32ADF365C6C1B7BD',
            'manufacturer': 'IBM',
            'model': 'IBM 350A',
            'capacity_bytes': 3750000000,
            'status': {
                'health': 'OK',
                'state': 'enabled'
            }
        }

        system_identity = 'ZZZ-YYY-XXX'
        mock_drive = mock.MagicMock(**attributes)
        mock_drive.name = attributes['name']
        mock_drive.status = mock.MagicMock(**attributes['status'])
        mock_storage = mock.MagicMock(
            spec=sushy.resources.system.storage.storage.Storage)
        mock_storage.drives = [mock_drive]
        mock_storage.identity = 'XXX-YYY-ZZZ'

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            # Test individual drive processing
            unique_name, sensor = task.driver.management._get_sensor_drive(
                mock_drive, mock_storage.identity, system_identity)

        expected_name = '32ADF365C6C1B7BD:XXX-YYY-ZZZ@ZZZ-YYY-XXX'
        expected_sensor = {
            'capacity_bytes': 3750000000,
            'health': 'OK',
            'name': '32ADF365C6C1B7BD',
            'model': 'IBM 350A',
            'state': 'enabled'
        }

        self.assertEqual(expected_name, unique_name)
        self.assertEqual(expected_sensor, sensor)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test__get_sensors_data(self, mock_get_system):
        # Mock the system object
        mock_system = mock.Mock()
        mock_system.manufacturer = 'Test Manufacturer'
        mock_system.model = 'Test Model'
        mock_system.uuid = 'test-uuid-ffff'
        mock_system.get_chassis_links.return_value = \
            ['/redfish/v1/Chassis/1']
        mock_system.get_storage_link.return_value = \
            '/redfish/v1/Systems/1/Storage'
        # Mock SimpleStorage not available, should fall back to Storage
        mock_system.simple_storage = None
        mock_system.get_simple_storage_link.side_effect = \
            sushy.exceptions.MissingAttributeError
        mock_get_system.return_value = mock_system

        with mock.patch.object(redfish_mgmt.RedfishManagement,
                               '_process_chassis_sensors',
                               autospec=True) as mock_chassis, \
             mock.patch.object(redfish_mgmt.RedfishManagement,
                               '_process_simple_storage_sensors',
                               autospec=True) as mock_simple_storage, \
             mock.patch.object(redfish_mgmt.RedfishManagement,
                               '_process_storage_sensors',
                               autospec=True) as mock_drives:

            # Make SimpleStorage raise MissingAttributeError to
            # fall back to Storage
            mock_simple_storage.side_effect = \
                sushy.exceptions.MissingAttributeError

            mock_chassis.return_value = {
                'Fan': {
                    '0@System.Embedded.1': {
                        'identity': '0', 'max_reading_range': None,
                        'min_reading_range': None, 'reading': 8640,
                        'reading_units': 'RPM', 'serial_number': None,
                        'physical_context': 'SystemBoard', 'state': 'Enabled',
                        'health': 'OK'
                    },
                    '1@System.Embedded.1': {
                        'identity': '1', 'max_reading_range': None,
                        'min_reading_range': None, 'reading': 7440,
                        'reading_units': 'RPM', 'serial_number': None,
                        'physical_context': 'SystemBoard', 'state': 'Enabled',
                        'health': 'OK'
                    }
                },
                'Temperature': {
                    '0@System.Embedded.1': {
                        'identity': '0', 'max_reading_range_temp': None,
                        'min_reading_range_temp': None, 'reading_celsius': 39,
                        'physical_context': 'CPU', 'sensor_number': 1,
                        'state': 'Enabled', 'health': 'OK'
                    },
                    '1@System.Embedded.1': {
                        'identity': '1', 'max_reading_range_temp': None,
                        'min_reading_range_temp': None, 'reading_celsius': 38,
                        'physical_context': 'CPU', 'sensor_number': 2,
                        'state': 'Enabled', 'health': 'OK'
                    }
                },
                'Power': {
                    '0:Power@System.Embedded.1': {
                        'power_capacity_watts': 1400,
                        'line_input_voltage': 204,
                        'last_power_output_watts': 394,
                        'serial_number': 'CNLOD0016Q2E47',
                        'state': 'Enabled', 'health': 'OK'
                    }
                }
            }
            mock_drives.return_value = {
                'Drive': {
                    'Solid_State_Disk_0_1_0_RAID_SL_3_1_System_Embedded_1': {
                        'name': 'Solid State Disk 0:1:0',
                        'model': 'MTFDDAK480TDT',
                        'capacity_bytes': 479559942144, 'state': 'Enabled',
                        'health': 'OK'
                    },
                    'Solid_State_Disk_0_1_1_RAID_SL_3_1_System_Embedded_1': {
                        'name': 'Solid State Disk 0:1:1',
                        'model': 'MTFDDAK480TDT',
                        'capacity_bytes': 479559942144, 'state': 'Enabled',
                        'health': 'OK'
                    }
                }
            }

            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=True) as task:
                result = task.driver.management.get_sensors_data(task)

            # Verify all methods called
            self.assertTrue(mock_chassis.called)
            self.assertTrue(mock_simple_storage.called)
            self.assertTrue(mock_drives.called)
            # Verify the call arguments (with autospec=True, self is first arg)
            chassis_args = mock_chassis.call_args[0]
            self.assertEqual(len(chassis_args), 3)  # self, node, system
            # Verify second arg is a node object and third arg is system
            self.assertIsNotNone(chassis_args[1])  # node should exist
            self.assertEqual(chassis_args[1].uuid, self.node.uuid)  # same node
            self.assertEqual(chassis_args[2], mock_system)  # system object
            # SimpleStorage should be called first (even though it fails)
            simple_storage_args = mock_simple_storage.call_args[0]
            self.assertEqual(len(simple_storage_args), 3)  # self, node, system
            self.assertEqual(simple_storage_args[1].uuid, self.node.uuid)
            self.assertEqual(simple_storage_args[2], mock_system)
            # Storage should be called as fallback
            drives_args = mock_drives.call_args[0]
            self.assertEqual(len(drives_args), 3)  # self, node, system
            self.assertIsNotNone(drives_args[1])  # node should exist
            self.assertEqual(drives_args[1].uuid, self.node.uuid)  # same node
            self.assertEqual(drives_args[2], mock_system)  # system object

            # Verify result structure and sample data
            self.assertIn('Fan', result)
            self.assertIn('Temperature', result)
            self.assertIn('Power', result)
            self.assertIn('Drive', result)
            self.assertIn('Extra', result)

            # Verify Extra metadata
            self.assertEqual(
                result['Extra']['Manufacturer'], 'Test Manufacturer')
            self.assertEqual(result['Extra']['Model'], 'Test Model')
            self.assertEqual(result['Extra']['UUID'], 'test-uuid-ffff')

            # Verify fan data
            self.assertEqual(len(result['Fan']), 2)
            self.assertEqual(
                result['Fan']['0@System.Embedded.1']['reading'], 8640)
            self.assertEqual(
                result['Fan']['1@System.Embedded.1']['reading'], 7440)

            # Verify temperature data
            self.assertEqual(len(result['Temperature']), 2)
            self.assertEqual(
                result['Temperature']['0@System.Embedded.1'][
                    'reading_celsius'],
                39)
            self.assertEqual(
                result['Temperature']['1@System.Embedded.1'][
                    'reading_celsius'],
                38)

            # Verify power data
            self.assertEqual(len(result['Power']), 1)
            power_data = result['Power']['0:Power@System.Embedded.1']
            self.assertEqual(power_data['power_capacity_watts'], 1400)
            self.assertEqual(power_data['last_power_output_watts'], 394)

            # Verify drive data
            self.assertEqual(len(result['Drive']), 2)
            drive1 = result['Drive'][
                'Solid_State_Disk_0_1_0_RAID_SL_3_1_System_Embedded_1']
            drive2 = result['Drive'][
                'Solid_State_Disk_0_1_1_RAID_SL_3_1_System_Embedded_1']
            self.assertEqual(drive1['capacity_bytes'], 479559942144)
            self.assertEqual(drive2['model'], 'MTFDDAK480TDT')

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test__get_sensors_data_simple_storage_priority(self, mock_get_system):
        # Mock the system object with SimpleStorage available
        mock_system = mock.Mock()
        mock_system.get_chassis_links.return_value = \
            ['/redfish/v1/Chassis/1']
        mock_system.get_storage_link.return_value = \
            '/redfish/v1/Systems/1/Storage'
        # Mock SimpleStorage available with drives
        mock_system.simple_storage = mock.Mock()
        mock_get_system.return_value = mock_system

        with mock.patch.object(redfish_mgmt.RedfishManagement,
                               '_process_chassis_sensors',
                               autospec=True) as mock_chassis, \
             mock.patch.object(redfish_mgmt.RedfishManagement,
                               '_process_simple_storage_sensors',
                               autospec=True) as mock_simple_storage, \
             mock.patch.object(redfish_mgmt.RedfishManagement,
                               '_process_storage_sensors',
                               autospec=True) as mock_drives:

            mock_chassis.return_value = {
                'Fan': {},
                'Temperature': {},
                'Power': {}
            }
            # SimpleStorage returns drives
            mock_simple_storage.return_value = {
                'Drive': {
                    'SSD1_SimpleStorage1_System_Embedded_1': {
                        'name': 'SSD1',
                        'model': 'Samsung SSD',
                        'capacity_bytes': 256000000000,
                        'state': 'Enabled',
                        'health': 'OK'
                    }
                }
            }
            # Storage method should not be called
            mock_drives.return_value = {
                'Drive': {}
            }

            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=True) as task:
                result = task.driver.management.get_sensors_data(task)

            # Verify chassis method called
            self.assertTrue(mock_chassis.called)
            # Verify SimpleStorage method called
            self.assertTrue(mock_simple_storage.called)
            # Verify Storage method NOT called (SimpleStorage provided drives)
            self.assertFalse(mock_drives.called)

            # Verify result structure and sample data
            self.assertIn('Fan', result)
            self.assertIn('Temperature', result)
            self.assertIn('Power', result)
            self.assertIn('Drive', result)

            # Verify drive data from SimpleStorage
            self.assertEqual(len(result['Drive']), 1)
            drive = result['Drive']['SSD1_SimpleStorage1_System_Embedded_1']
            self.assertEqual(drive['name'], 'SSD1')
            self.assertEqual(drive['model'], 'Samsung SSD')
            self.assertEqual(drive['capacity_bytes'], 256000000000)

    def test__sensor2dict_fan_data(self):
        mock_fan = mock.Mock()
        mock_fan.identity = 'XXX-YYY-ZZZ'
        mock_fan.reading = 6000
        mock_fan.reading_units = sushy_thermal_const.FanReadingUnit.RPM
        mock_fan.serial_number = 'SN010203040506'
        mock_fan.physical_context = 'CPU'
        mock_fan.min_reading_range = 0
        mock_fan.max_reading_range = 10000
        mock_fan.status = mock.Mock()
        mock_fan.status.state = sushy_constants.State.ENABLED
        mock_fan.status.health = sushy_constants.Health.OK

        result = redfish_mgmt.RedfishManagement._sensor2dict(
            mock_fan, 'identity', 'reading', 'reading_units', 'serial_number',
            'physical_context', 'min_reading_range', 'max_reading_range'
        )
        result.update(redfish_mgmt.RedfishManagement._sensor2dict(
            mock_fan.status, 'state', 'health'))

        expected = {
            'identity': 'XXX-YYY-ZZZ',
            'reading': 6000,
            'reading_units': 'RPM',
            'serial_number': 'SN010203040506',
            'physical_context': 'CPU',
            'min_reading_range': 0,
            'max_reading_range': 10000,
            'state': 'Enabled',
            'health': 'OK',
        }

        self.assertEqual(result, expected)

    def test__sensor2dict_temperature_data(self):
        mock_chassis = mock.MagicMock(identity='ZZZ-YYY-XXX')
        mock_temp = mock.Mock()
        mock_temp.identity = 'XXX-YYY-ZZZ'
        mock_temp.reading_celsius = 62
        mock_temp.name = "CPU Temp"
        mock_temp.upper_threshold_non_critical = 75
        mock_temp.upper_threshold_critical = 90
        mock_temp.upperThresholdFatal = 95
        mock_temp.min_reading_range_temp = 0
        mock_temp.max_reading_range_temp = 120
        mock_temp.physical_context = "CPU"
        mock_temp.sensor_number = 1
        mock_temp.status = mock.Mock()
        mock_temp.status.state = sushy.resources.constants.State.ENABLED
        mock_temp.status.health = sushy.resources.constants.Health.OK
        mock_chassis.thermal.temperatures = [mock_temp]

        sensor = redfish_mgmt.RedfishManagement._sensor2dict(
            mock_temp, 'identity', 'max_reading_range_temp',
            'min_reading_range_temp', 'reading_celsius',
            'physical_context', 'sensor_number'
        )

        sensor.update(redfish_mgmt.RedfishManagement._sensor2dict(
            mock_temp.status, 'state', 'health'))

        unique_name = '%s@%s' % (mock_temp.identity, mock_chassis.identity)
        result = {unique_name: sensor}

        expected = {
            'XXX-YYY-ZZZ@ZZZ-YYY-XXX': {
                'identity': 'XXX-YYY-ZZZ',
                'max_reading_range_temp': 120,
                'min_reading_range_temp': 0,
                'physical_context': 'CPU',
                'reading_celsius': 62,
                'sensor_number': 1,
                'health': 'OK',
                'state': 'Enabled'
            }
        }

        self.assertEqual(result, expected)

    def test__sensor2dict_power_data(self):
        mock_chassis = mock.MagicMock(identity='ZZZ-YYY-XXX')
        mock_power = mock_chassis.power
        mock_power.identity = 'Power'
        mock_psu = mock.Mock()
        mock_psu.identity = 0
        mock_psu.name = 'Power Supply 0'
        mock_psu.power_capacity_watts = 1450
        mock_psu.last_power_output_watts = 650
        mock_psu.line_input_voltage = 220
        mock_psu.serial_number = 'SN010203040506'
        mock_psu.status = mock.Mock()
        mock_psu.status.state = sushy_constants.State.ENABLED
        mock_psu.status.health = sushy_constants.Health.OK
        mock_power.power_supplies = [mock_psu]
        sensor = redfish_mgmt.RedfishManagement._sensor2dict(
            mock_psu, 'power_capacity_watts',
            'line_input_voltage', 'last_power_output_watts',
            'serial_number'
        )
        sensor.update(redfish_mgmt.RedfishManagement._sensor2dict(
            mock_psu.status, 'state', 'health'))
        unique_name = '%s:%s@%s' % (mock_psu.identity, mock_power.identity,
                                    mock_chassis.identity)
        result = {unique_name: sensor}

        expected = {
            '0:Power@ZZZ-YYY-XXX': {
                'health': 'OK',
                'last_power_output_watts': 650,
                'line_input_voltage': 220,
                'power_capacity_watts': 1450,
                'serial_number': 'SN010203040506',
                'state': 'Enabled'
            }
        }
        self.assertEqual(result, expected)

    def test__sensor2dict_data_drive_storage(self):
        mock_system = mock.MagicMock(spec=sushy.resources.system.system.System)
        mock_system.identity = 'ZZZ-YYY-XXX'
        mock_drive = mock.Mock()
        mock_drive.name = '32ADF365C6C1B7BD'
        mock_drive.manufacturer = 'IBM'
        mock_drive.model = 'IBM 350A'
        mock_drive.capacity_bytes = 3750000000
        mock_drive.status = mock.Mock()
        mock_drive.status.state = sushy_constants.State.ENABLED
        mock_drive.status.health = sushy_constants.Health.OK
        mock_simple_storage = mock.MagicMock(
            spec=sushy.resources.system.simple_storage.SimpleStorage)
        mock_simple_storage.devices = [mock_drive]
        mock_simple_storage.identity = 'XXX-YYY-ZZZ'
        mock_system.simple_storage.get_members.return_value = [
            mock_simple_storage]
        mock_system.storage = {}

        sensor = redfish_mgmt.RedfishManagement._sensor2dict(
            mock_drive, 'name', 'model', 'capacity_bytes')
        sensor.update(redfish_mgmt.RedfishManagement._sensor2dict(
            mock_drive.status, 'state', 'health'))
        # Use actual implementation format (colons and @ symbols)
        unique_name = '%s:%s@%s' % (
            mock_drive.name, mock_simple_storage.identity,
            mock_system.identity)
        result = {unique_name: sensor}
        expected = {
            '32ADF365C6C1B7BD:XXX-YYY-ZZZ@ZZZ-YYY-XXX': {
                'capacity_bytes': 3750000000,
                'health': 'OK',
                'name': '32ADF365C6C1B7BD',
                'model': 'IBM 350A',
                'state': 'Enabled'
            }
        }

        self.assertEqual(result, expected)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_node_health_ok(self, mock_get_system):
        """Test get_node_health returns OK when system health is OK."""
        mock_system = mock.Mock()
        mock_system.status = mock.Mock()
        mock_system.status.health = sushy.HEALTH_OK
        mock_get_system.return_value = mock_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management.get_node_health(task)
            self.assertEqual(health_states.HealthState.OK, result)
            mock_get_system.assert_called_once_with(task.node)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_node_health_warning(self, mock_get_system):
        """Test get_node_health returns Warning for Warning health."""
        mock_system = mock.Mock()
        mock_system.status = mock.Mock()
        mock_system.status.health = sushy.HEALTH_WARNING
        mock_get_system.return_value = mock_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management.get_node_health(task)
            self.assertEqual(health_states.HealthState.WARNING, result)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_node_health_critical(self, mock_get_system):
        """Test get_node_health returns Critical for Critical health."""
        mock_system = mock.Mock()
        mock_system.status = mock.Mock()
        mock_system.status.health = sushy.HEALTH_CRITICAL
        mock_get_system.return_value = mock_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management.get_node_health(task)
            self.assertEqual(health_states.HealthState.CRITICAL, result)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_node_health_status_none(self, mock_get_system):
        """Test get_node_health returns None when system.status is None."""
        mock_system = mock.Mock()
        mock_system.status = None
        mock_get_system.return_value = mock_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management.get_node_health(task)
            self.assertIsNone(result)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_node_health_health_none(self, mock_get_system):
        """Test get_node_health returns None when status.health is None."""
        mock_system = mock.Mock()
        mock_system.status = mock.Mock()
        mock_system.status.health = None
        mock_get_system.return_value = mock_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management.get_node_health(task)
            self.assertIsNone(result)

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test_get_node_health_unknown_value(self, mock_get_system):
        """Test get_node_health returns None for unmapped health values."""
        mock_system = mock.Mock()
        mock_system.status = mock.Mock()
        # Use a value that's not in HEALTH_MAP
        mock_system.status.health = 'UnknownHealthValue'
        mock_get_system.return_value = mock_system

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management.get_node_health(task)
            # HEALTH_MAP.get() returns None for unknown values
            self.assertIsNone(result)

    def test__sensor2dict_enum_handling(self):
        """Test that enum values like Health.OK are converted to strings."""
        # Create a mock object with enum attributes
        mock_resource = mock.Mock()
        mock_resource.name = 'Test Drive'
        mock_resource.status = mock.Mock()

        # Set enum values that have both 'value' and 'name' attributes
        mock_resource.status.health = sushy_constants.Health.OK
        mock_resource.status.state = sushy_constants.State.ENABLED

        # Test the _sensor2dict method
        result = redfish_mgmt.RedfishManagement._sensor2dict(
            mock_resource, 'name')
        result.update(redfish_mgmt.RedfishManagement._sensor2dict(
            mock_resource.status, 'health', 'state'))

        # Verify that enum values are converted to strings
        expected = {
            'name': 'Test Drive',
            'health': 'OK',  # Should be string, not enum
            'state': 'Enabled'  # Should be string, not enum
        }

        self.assertEqual(result, expected)

        # Verify the values are actually strings, not enum objects
        self.assertIsInstance(result['health'], str)
        self.assertIsInstance(result['state'], str)
        self.assertEqual(result['health'], 'OK')
        self.assertEqual(result['state'], 'Enabled')

    def test__get_sensor_drive(self):
        """Test _get_sensor_drive method for individual drive processing."""
        # Create a mock drive
        mock_drive = mock.Mock()
        mock_drive.name = 'TestDrive1'
        mock_drive.model = 'Test Model XYZ'
        mock_drive.capacity_bytes = 500000000
        mock_drive.status = mock.Mock()
        mock_drive.status.health = sushy_constants.Health.OK
        mock_drive.status.state = sushy_constants.State.ENABLED

        # Test the method
        unique_name, sensor_data = (
            redfish_mgmt.RedfishManagement._get_sensor_drive(
                mock_drive, 'storage_id', 'system_id'))

        # Verify the unique name format
        expected_name = 'TestDrive1:storage_id@system_id'
        self.assertEqual(unique_name, expected_name)

        # Verify the sensor data
        expected_sensor = {
            'name': 'TestDrive1',
            'model': 'Test Model XYZ',
            'capacity_bytes': 500000000,
            'health': 'OK',
            'state': 'Enabled'
        }
        self.assertEqual(sensor_data, expected_sensor)

    def test__get_sensor_drive_no_state(self):
        """Test _get_sensor_drive when state is None (HPE workaround)."""
        mock_drive = mock.Mock()
        mock_drive.name = 'Samsung SSD'
        mock_drive.model = 'VR000480KXLXF'
        mock_drive.capacity_bytes = 480103981056
        mock_drive.status = mock.Mock()
        mock_drive.status.health = sushy_constants.Health.OK
        mock_drive.status.state = None  # HPE drives may return None

        unique_name, sensor_data = (
            redfish_mgmt.RedfishManagement._get_sensor_drive(
                mock_drive, 'DA011400', 'System1'))

        self.assertEqual(unique_name, 'Samsung SSD:DA011400@System1')
        # State should default to 'Enabled' when None but health is present
        self.assertEqual(sensor_data['state'], 'Enabled')
        self.assertEqual(sensor_data['health'], 'OK')

    def test__process_chassis_sensors(self):
        mock_chassis = mock.MagicMock()
        mock_chassis.identity = '/redfish/v1/Chassis/1'

        # Setup minimal thermal and power data
        mock_chassis.thermal.fans = []
        mock_chassis.thermal.temperatures = []
        mock_chassis.power.power_supplies = []

        mock_system = mock.MagicMock()
        mock_system.chassis_expanded = [mock_chassis]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management._process_chassis_sensors(
                task.node, mock_system)

        expected = {'Fan': {}, 'Temperature': {}, 'Power': {}}
        self.assertEqual(result, expected)

    def test__process_chassis_sensors_sushy_error(self):
        # Test that when chassis_expanded raises SushyError, we handle
        # it gracefully
        mock_system = mock.MagicMock()
        mock_system.chassis_expanded = []  # Empty chassis list

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management._process_chassis_sensors(
                task.node, mock_system)

        expected = {'Fan': {}, 'Temperature': {}, 'Power': {}}
        self.assertEqual(result, expected)

    @mock.patch.object(redfish_utils, 'parse_driver_info', autospec=True)
    def test__process_storage_sensors(self, mock_parse_driver):
        mock_parse_driver.return_value = {
            'system_id': '/redfish/v1/Systems/1'
        }

        mock_storage_collection = mock.MagicMock()
        mock_storage_collection.get_members.return_value = []

        mock_system = mock.MagicMock()
        mock_system.storage_expanded = mock_storage_collection

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management._process_storage_sensors(
                task.node, mock_system)

        expected = {'Drive': {}}
        self.assertEqual(result, expected)


class AsyncSensorDataTestCase(db_base.DbTestCase):

    def setUp(self):
        super(AsyncSensorDataTestCase, self).setUp()
        self.config(enabled_hardware_types=['redfish'],
                    enabled_power_interfaces=['redfish'],
                    enabled_boot_interfaces=['redfish-virtual-media'],
                    enabled_management_interfaces=['redfish'],
                    enabled_inspect_interfaces=['redfish'],
                    enabled_bios_interfaces=['redfish'])
        self.node = obj_utils.create_test_node(
            self.context, driver='redfish', driver_info=INFO_DICT)

    def test_get_sensors_data_async_returns_coroutine(self):
        """Test that get_sensors_data_async returns a coroutine."""
        # the conductor hands over the whole node, not a hand-picked
        # subset of its fields
        node_dict = self.node.as_dict()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            result = task.driver.management.get_sensors_data_async(node_dict)
            # Should return a coroutine, not raise NotImplementedError
            self.assertTrue(asyncio.iscoroutine(result))
            # Clean up the coroutine to avoid RuntimeWarning
            result.close()

    def test_get_sensors_data_async_bad_driver_info(self):
        """Driver info is validated before the event loop runs."""
        node_dict = self.node.as_dict()
        node_dict['driver_info'] = {}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertRaises(
                exception.MissingParameterValue,
                task.driver.management.get_sensors_data_async, node_dict)

    def _collect_async_sensors(self, client, driver_info=None):
        """Run the async collector against a mocked Redfish client."""
        @contextlib.asynccontextmanager
        async def _session(_driver_info):
            yield client

        mgmt = redfish_mgmt.RedfishManagement()
        with mock.patch.object(redfish_utils, 'async_session', _session):
            return asyncio.run(mgmt._get_sensors_data_async_impl(
                {'uuid': 'node-uuid'}, driver_info or {}))

    def test_sensor_data_async_several_systems(self):
        """Several systems must be reported in operator terms."""
        client = mock.Mock()
        client.collection = mock.AsyncMock(
            return_value=async_resource.AsyncResourceCollection(
                mock.Mock(), ['/redfish/v1/Systems/1',
                              '/redfish/v1/Systems/2']))

        exc = self.assertRaises(exception.FailedToGetSensorData,
                                self._collect_async_sensors, client)

        # not the bare 'expected exactly 1 item in collection' from the
        # Redfish layer, and not labelled an unexpected error either
        self.assertIn('manages 2 systems', str(exc))
        self.assertIn('redfish_system_id', str(exc))
        self.assertNotIn('Unexpected error', str(exc))

    def test_sensor2dict_async_fan(self):
        """A fan payload uses the same keys and types as sushy.

        Regression test: MemberId must surface as ``identity``, the key
        the synchronous path emits, not as ``member_id``.
        """
        data = {
            'MemberId': '0',
            'Reading': 6000,
            'ReadingUnits': 'RPM',
            'PhysicalContext': 'SystemBoard',
            'Name': 'CPU Fan',
        }
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True):
            result = redfish_mgmt._sensor2dict_async(
                data, redfish_mgmt._FAN_FIELDS)

        # Keys the BMC did not send are reported as None rather than
        # omitted, as _sensor2dict does via hasattr on a sushy resource.
        expected = {
            'identity': '0',
            'max_reading_range': None,
            'min_reading_range': None,
            'reading': 6000,
            'reading_units': 'RPM',
            'serial_number': None,
            'physical_context': 'SystemBoard',
        }
        self.assertEqual(expected, result)

    def test_sensor2dict_async_coerces_numbers(self):
        """Numeric fields are cast with sushy's own int_or_none."""
        data = {'MemberId': '0', 'Reading': '6000',
                'MaxReadingRange': 12000.0}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True):
            result = redfish_mgmt._sensor2dict_async(
                data, redfish_mgmt._FAN_FIELDS)

        self.assertEqual(6000, result['reading'])
        self.assertEqual(12000, result['max_reading_range'])

    def test_sensor2dict_async_rejects_bad_number(self):
        """A value sushy would reject is rejected here too.

        Using sushy's adapter rather than a lenient copy keeps the two
        paths agreeing on bad input: the caller drops the whole sensor
        group, instead of async alone reporting the field as None.
        """
        data = {'MemberId': '0', 'Reading': 'bogus'}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True):
            self.assertRaises(
                ValueError,
                redfish_mgmt._sensor2dict_async,
                data, redfish_mgmt._FAN_FIELDS)

    def test_sensor2dict_async_temperature(self):
        """A temperature payload matches the synchronous key set."""
        data = {'MemberId': '1', 'ReadingCelsius': 38,
                'PhysicalContext': 'CPU', 'SensorNumber': 2}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True):
            result = redfish_mgmt._sensor2dict_async(
                data, redfish_mgmt._TEMPERATURE_FIELDS)

        expected = {
            'identity': '1',
            'max_reading_range_temp': None,
            'min_reading_range_temp': None,
            'reading_celsius': 38,
            'physical_context': 'CPU',
            'sensor_number': 2,
        }
        self.assertEqual(expected, result)

    def test_sensor2dict_async_power_has_no_identity(self):
        """Power supplies carry no identity, matching the sync path.

        _get_sensors_power does not request the identity field, so
        adding one here would itself be a divergence.
        """
        data = {'MemberId': '0', 'PowerCapacityWatts': 1400,
                'LineInputVoltage': 204, 'LastPowerOutputWatts': 394,
                'SerialNumber': 'CNLOD0016Q2E47'}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True):
            result = redfish_mgmt._sensor2dict_async(
                data, redfish_mgmt._POWER_SUPPLY_FIELDS)

        self.assertNotIn('identity', result)
        self.assertNotIn('member_id', result)
        self.assertEqual({'power_capacity_watts': 1400,
                          'line_input_voltage': 204,
                          'last_power_output_watts': 394,
                          'serial_number': 'CNLOD0016Q2E47'}, result)

    def test_sensor2dict_async_empty_dict(self):
        """An empty body still yields the full key set, all None."""
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True):
            result = redfish_mgmt._sensor2dict_async(
                {}, redfish_mgmt._TEMPERATURE_FIELDS)

        self.assertEqual({'identity', 'max_reading_range_temp',
                          'min_reading_range_temp', 'reading_celsius',
                          'physical_context', 'sensor_number'},
                         set(result))
        self.assertEqual([None] * 6, list(result.values()))

    def test_sensor2dict_async_non_dict(self):
        """Test _sensor2dict_async with non-dict input."""
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True):
            result = redfish_mgmt._sensor2dict_async(
                'not a dict', redfish_mgmt._FAN_FIELDS)
            self.assertEqual({}, result)

    def test_status2dict_async_present(self):
        """State and health are read from the Status object."""
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True):
            result = redfish_mgmt._status2dict_async(
                {'Status': {'State': 'Enabled', 'Health': 'OK'}})

        self.assertEqual({'state': 'Enabled', 'health': 'OK'}, result)

    def test_status2dict_async_absent(self):
        """No Status object means no state/health keys at all.

        The sync path reads these off the sushy status composite field,
        which is None when the BMC sends no Status, so _sensor2dict
        contributes nothing.
        """
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True):
            self.assertEqual({}, redfish_mgmt._status2dict_async(
                {'MemberId': '0'}))
            self.assertEqual({}, redfish_mgmt._status2dict_async(
                {'Status': None}))

    def test_get_sensor_drive_async(self):
        """Test _get_sensor_drive_async with drive dict data."""
        drive_data = {
            'Name': 'Drive 0',
            'Model': 'Samsung SSD',
            'CapacityBytes': 500000000000,
            'Status': {
                'State': 'Enabled',
                'Health': 'OK',
            }
        }
        unique_name, sensor = redfish_mgmt._get_sensor_drive_async(
            drive_data, 'RAID.Integrated.1-1', '1')

        self.assertEqual('Drive 0:RAID.Integrated.1-1@1', unique_name)
        self.assertEqual('Samsung SSD', sensor['model'])
        self.assertEqual(500000000000, sensor['capacity_bytes'])
        self.assertEqual('Enabled', sensor['state'])
        self.assertEqual('OK', sensor['health'])

    def test_get_sensor_drive_async_no_state(self):
        """Test _get_sensor_drive_async without State but with Health."""
        drive_data = {
            'Name': 'Drive 0',
            'Model': 'Samsung SSD',
            'CapacityBytes': 500000000000,
            'Status': {
                'Health': 'OK',
            }
        }
        unique_name, sensor = redfish_mgmt._get_sensor_drive_async(
            drive_data, 'Storage1', 'System1')

        self.assertEqual('Enabled', sensor['state'])


class AsyncSensorPayloadParityTestCase(db_base.DbTestCase):
    """The async chassis payload must match the synchronous one.

    The async collector promises the same output format, but it builds
    payloads from raw Redfish JSON while the sync path goes through
    sushy. Nothing else compares the two, which is how the async path
    came to emit member_id where sushy emits identity, to drop keys for
    fields the BMC left out, and to skip sushy's int coercion.

    The expectations below are the sync payloads, lifted from
    RedfishManagementTestCase.test_get_sensors_data.
    """

    THERMAL = {
        'Fans': [{'MemberId': '0', 'Reading': 8640, 'ReadingUnits': 'RPM',
                  'PhysicalContext': 'SystemBoard',
                  'Status': {'State': 'Enabled', 'Health': 'OK'}}],
        'Temperatures': [{'MemberId': '0', 'ReadingCelsius': 39,
                          'PhysicalContext': 'CPU', 'SensorNumber': 1,
                          'Status': {'State': 'Enabled', 'Health': 'OK'}}],
    }
    POWER = {
        'Id': 'Power',
        'PowerSupplies': [{'MemberId': '0', 'PowerCapacityWatts': 1400,
                           'LineInputVoltage': 204,
                           'LastPowerOutputWatts': 394,
                           'SerialNumber': 'CNLOD0016Q2E47',
                           'Status': {'State': 'Enabled', 'Health': 'OK'}}],
    }
    # what a BMC that ignores $expand returns: bare references
    CHASSIS = {'Id': 'System.Embedded.1',
               'Thermal': {'@odata.id': '/t'},
               'Power': {'@odata.id': '/p'}}
    # what a BMC that honours it returns: the sub-resources inline
    EXPANDED_CHASSIS = {'Id': 'System.Embedded.1',
                        'Thermal': THERMAL,
                        'Power': POWER}

    @staticmethod
    def _response(body):
        """Build what AsyncConnector.get actually returns.

        It hands back the raw HTTP response, not the decoded body.
        Mocking it as the body hid a bug where the collector called
        ``.get`` straight on the response, so every chassis sensor was
        silently dropped against a real BMC.
        """
        return mock.Mock(json=mock.Mock(return_value=body), headers={})

    def _collect(self, chassis=None):
        return self._collect_with_requests(chassis)[0]

    def _collect_with_requests(self, chassis=None):
        """Collect chassis sensors, also reporting the requests made.

        :param chassis: the chassis body the BMC serves, defaulting to
            one that ignores ``$expand``.
        :returns: a (sensors, requested paths) tuple.
        """
        system = mock.Mock()
        system.field.return_value = [{'@odata.id': '/c'}]
        bodies = {'/c': self.CHASSIS if chassis is None else chassis,
                  '/t': self.THERMAL, '/p': self.POWER}
        # the chassis is requested with a $expand query string
        system._connector.get = mock.AsyncMock(
            side_effect=lambda path: self._response(
                bodies[path.split('?')[0]]))
        mgmt = redfish_mgmt.RedfishManagement()
        sensors = asyncio.run(
            mgmt._process_chassis_sensors_async('node-uuid', system))
        requested = [call.args[0]
                     for call in system._connector.get.call_args_list]
        return sensors, requested

    def test_fan_payload_matches_sync(self):
        self.assertEqual(
            {'0@System.Embedded.1': {
                'identity': '0', 'max_reading_range': None,
                'min_reading_range': None, 'reading': 8640,
                'reading_units': 'RPM', 'serial_number': None,
                'physical_context': 'SystemBoard', 'state': 'Enabled',
                'health': 'OK'}},
            self._collect()['Fan'])

    def test_temperature_payload_matches_sync(self):
        self.assertEqual(
            {'0@System.Embedded.1': {
                'identity': '0', 'max_reading_range_temp': None,
                'min_reading_range_temp': None, 'reading_celsius': 39,
                'physical_context': 'CPU', 'sensor_number': 1,
                'state': 'Enabled', 'health': 'OK'}},
            self._collect()['Temperature'])

    def test_power_payload_matches_sync(self):
        self.assertEqual(
            {'0:Power@System.Embedded.1': {
                'power_capacity_watts': 1400, 'line_input_voltage': 204,
                'last_power_output_watts': 394,
                'serial_number': 'CNLOD0016Q2E47',
                'state': 'Enabled', 'health': 'OK'}},
            self._collect()['Power'])

    def test_expanded_chassis_costs_one_request(self):
        """$expand must be used, as the sync path does, not 3 requests."""
        expanded, requested = self._collect_with_requests(
            self.EXPANDED_CHASSIS)

        self.assertEqual(['/c?$expand=.($levels=1)'], requested)
        # and the inlined sub-resources give the very same payload
        self.assertEqual(self._collect(), expanded)

    def test_falls_back_when_expand_ignored(self):
        """A BMC may ignore $expand and return bare references."""
        _sensors, requested = self._collect_with_requests()

        self.assertEqual(['/c?$expand=.($levels=1)', '/t', '/p'], requested)


class AsyncStorageSensorsTestCase(db_base.DbTestCase):
    """Drive collection over the async client.

    Both storage paths were only reached incidentally through
    _get_sensors_data_async_impl, so the branches that matter -- the
    SimpleStorage shortcut, skipping devices that are not drives, and
    carrying on past one unreadable drive -- went unexercised.

    The connector is the only thing mocked: real AsyncResource objects
    do the link following and collection iteration, so a change in that
    plumbing shows up here.
    """

    SYSTEM_PATH = '/redfish/v1/Systems/1'

    @staticmethod
    def _response(body):
        """What AsyncConnector.get returns: the response, not the body."""
        return mock.Mock(json=mock.Mock(return_value=body), headers={})

    def _system(self, system_body, bodies):
        """Build a system resource served by a recording connector.

        :param system_body: JSON body of the system itself.
        :param bodies: paths to JSON bodies the BMC serves, looked up
            with any query string stripped.
        :returns: a (system, requested paths list) tuple.
        """
        requested = []

        async def _get(path, **kwargs):
            requested.append(path)
            try:
                return self._response(bodies[path.split('?')[0]])
            except KeyError:
                raise async_exceptions.ResourceNotFoundError(
                    method='GET', url=path, response=None)

        connector = mock.Mock()
        connector.get = mock.AsyncMock(side_effect=_get)
        system = async_resource.AsyncResource(
            connector, self.SYSTEM_PATH, system_body)
        return system, requested

    def _collect_storage(self, system_body, bodies):
        mgmt = redfish_mgmt.RedfishManagement()
        system, requested = self._system(system_body, bodies)
        sensors = asyncio.run(
            mgmt._process_storage_sensors_async('node-uuid', system))
        return sensors['Drive'], requested

    def _collect_simple_storage(self, system_body, bodies):
        mgmt = redfish_mgmt.RedfishManagement()
        system, requested = self._system(system_body, bodies)
        sensors = asyncio.run(
            mgmt._process_simple_storage_sensors_async('node-uuid', system))
        return sensors['Drive'], requested

    # --- Storage ---

    def test_storage_drives_are_collected(self):
        drives, _ = self._collect_storage(
            {'Storage': {'@odata.id': '/st'}},
            {'/st': {'Members': [{'@odata.id': '/st/1'}]},
             '/st/1': {'Id': 'RAID', 'Drives': [{'@odata.id': '/st/1/d0'}]},
             '/st/1/d0': {'Name': 'Disk 0', 'Model': 'ST1000',
                          'CapacityBytes': 1000,
                          'Status': {'State': 'Enabled', 'Health': 'OK'}}})

        self.assertEqual(
            {'Disk 0:RAID@1': {'name': 'Disk 0', 'model': 'ST1000',
                               'capacity_bytes': 1000,
                               'state': 'Enabled', 'health': 'OK'}},
            drives)

    def test_storage_drive_reference_without_a_path_is_skipped(self):
        """A malformed member must not cost the drives beside it."""
        drives, _ = self._collect_storage(
            {'Storage': {'@odata.id': '/st'}},
            {'/st': {'Members': [{'@odata.id': '/st/1'}]},
             '/st/1': {'Id': 'RAID',
                       'Drives': [{}, 'not-a-dict',
                                  {'@odata.id': '/st/1/d0'}]},
             '/st/1/d0': {'Name': 'Disk 0', 'CapacityBytes': 1000}})

        self.assertEqual(['Disk 0:RAID@1'], list(drives))

    def test_storage_unreadable_drive_does_not_lose_the_controller(self):
        """One drive the BMC will not serve must not drop the rest.

        The drives of a controller are fetched one at a time, so a
        single bad response would otherwise abandon the controller.
        """
        drives, _ = self._collect_storage(
            {'Storage': {'@odata.id': '/st'}},
            {'/st': {'Members': [{'@odata.id': '/st/1'},
                                 {'@odata.id': '/st/2'}]},
             '/st/1': {'Id': 'RAID', 'Drives': [{'@odata.id': '/missing'}]},
             '/st/2': {'Id': 'HBA', 'Drives': [{'@odata.id': '/st/2/d0'}]},
             '/st/2/d0': {'Name': 'Disk 9', 'CapacityBytes': 2000}})

        self.assertEqual(['Disk 9:HBA@1'], list(drives))

    def test_storage_missing_link_reports_no_drives(self):
        drives, _ = self._collect_storage({}, {})

        self.assertEqual({}, drives)

    def test_storage_is_requested_expanded(self):
        """$expand is asked for, as the synchronous path does."""
        _drives, requested = self._collect_storage(
            {'Storage': {'@odata.id': '/st'}}, {'/st': {'Members': []}})

        self.assertEqual(['/st?$expand=.($levels=1)'], requested)

    # --- SimpleStorage ---

    def test_simple_storage_devices_are_collected_inline(self):
        """Devices come inline, so no request per drive is made."""
        drives, requested = self._collect_simple_storage(
            {'SimpleStorage': {'@odata.id': '/ss'}},
            {'/ss': {'Members': [{'@odata.id': '/ss/1'}]},
             '/ss/1': {'Id': 'Ctrl', 'Devices': [
                 {'Name': 'Disk 0', 'Model': 'ST1000',
                  'CapacityBytes': 1000,
                  'Status': {'State': 'Enabled', 'Health': 'OK'}}]}})

        self.assertEqual(
            {'Disk 0:Ctrl@1': {'name': 'Disk 0', 'model': 'ST1000',
                               'capacity_bytes': 1000,
                               'state': 'Enabled', 'health': 'OK'}},
            drives)
        # the collection and its one member, and nothing per device
        self.assertEqual(['/ss?$expand=.($levels=1)', '/ss/1'], requested)

    def test_simple_storage_skips_devices_without_capacity(self):
        """Backplanes and enclosures are not drives.

        They carry no capacity_bytes, and reporting them makes the
        Prometheus exporter complain about inconsistent labels.
        """
        drives, _ = self._collect_simple_storage(
            {'SimpleStorage': {'@odata.id': '/ss'}},
            {'/ss': {'Members': [{'@odata.id': '/ss/1'}]},
             '/ss/1': {'Id': 'Ctrl', 'Devices': [
                 {'Name': 'Backplane'},
                 {'Name': 'Enclosure', 'CapacityBytes': 0},
                 {'Name': 'Disk 0', 'CapacityBytes': 1000}]}})

        self.assertEqual(['Disk 0:Ctrl@1'], list(drives))

    def test_simple_storage_absent_raises_for_the_caller(self):
        """The caller falls back to Storage on this."""
        mgmt = redfish_mgmt.RedfishManagement()
        system, _ = self._system({}, {})

        self.assertRaises(
            ValueError, asyncio.run,
            mgmt._process_simple_storage_sensors_async('node-uuid', system))

    def test_simple_storage_non_dict_device_is_skipped(self):
        drives, _ = self._collect_simple_storage(
            {'SimpleStorage': {'@odata.id': '/ss'}},
            {'/ss': {'Members': [{'@odata.id': '/ss/1'}]},
             '/ss/1': {'Id': 'Ctrl',
                       'Devices': ['not-a-dict',
                                   {'Name': 'Disk 0', 'CapacityBytes': 1}]}})

        self.assertEqual(['Disk 0:Ctrl@1'], list(drives))
