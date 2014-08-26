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
Test class for common methods used by DRAC modules.
"""

from xml.etree import ElementTree

from ironic.common import exception
from ironic.drivers.modules.drac import common as drac_common
from ironic.openstack.common import context
from ironic.tests import base
from ironic.tests.db import utils as db_utils
from ironic.tests.objects import utils as obj_utils

INFO_DICT = db_utils.get_test_drac_info()


class DracCommonMethodsTestCase(base.TestCase):

    def setUp(self):
        super(DracCommonMethodsTestCase, self).setUp()
        self.context = context.get_admin_context()

    def test_parse_driver_info(self):
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_drac',
                                          driver_info=INFO_DICT)
        info = drac_common.parse_driver_info(node)

        self.assertIsNotNone(info.get('drac_host'))
        self.assertIsNotNone(info.get('drac_port'))
        self.assertIsNotNone(info.get('drac_path'))
        self.assertIsNotNone(info.get('drac_protocol'))
        self.assertIsNotNone(info.get('drac_username'))
        self.assertIsNotNone(info.get('drac_password'))

    def test_parse_driver_info_missing_host(self):
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_drac',
                                          driver_info=INFO_DICT)
        del node.driver_info['drac_host']
        self.assertRaises(exception.InvalidParameterValue,
                          drac_common.parse_driver_info, node)

    def test_parse_driver_info_missing_port(self):
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_drac',
                                          driver_info=INFO_DICT)
        del node.driver_info['drac_port']

        info = drac_common.parse_driver_info(node)
        self.assertEqual(443, info.get('drac_port'))

    def test_parse_driver_info_invalid_port(self):
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_drac',
                                          driver_info=INFO_DICT)
        node.driver_info['drac_port'] = 'foo'
        self.assertRaises(exception.InvalidParameterValue,
                          drac_common.parse_driver_info, node)

    def test_parse_driver_info_missing_path(self):
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_drac',
                                          driver_info=INFO_DICT)
        del node.driver_info['drac_path']

        info = drac_common.parse_driver_info(node)
        self.assertEqual('/wsman', info.get('drac_path'))

    def test_parse_driver_info_missing_protocol(self):
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_drac',
                                          driver_info=INFO_DICT)
        del node.driver_info['drac_protocol']

        info = drac_common.parse_driver_info(node)
        self.assertEqual('https', info.get('drac_protocol'))

    def test_parse_driver_info_missing_username(self):
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_drac',
                                          driver_info=INFO_DICT)
        del node.driver_info['drac_username']
        self.assertRaises(exception.InvalidParameterValue,
                          drac_common.parse_driver_info, node)

    def test_parse_driver_info_missing_password(self):
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_drac',
                                          driver_info=INFO_DICT)
        del node.driver_info['drac_password']
        self.assertRaises(exception.InvalidParameterValue,
                          drac_common.parse_driver_info, node)

    def test_find_xml(self):
        namespace = 'http://fake'
        value = 'fake_value'
        test_doc = ElementTree.fromstring("""<Envelope xmlns:ns1="%(ns)s">
                         <Body>
                           <ns1:test_element>%(value)s</ns1:test_element>
                         </Body>
                       </Envelope>""" % {'ns': namespace, 'value': value})

        result = drac_common.find_xml(test_doc, 'test_element', namespace)
        self.assertEqual(value, result.text)
