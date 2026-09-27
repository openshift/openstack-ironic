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

from oslo_config import cfg

from ironic.common.i18n import _

opts = [
    cfg.BoolOpt('send_sensor_data',
                default=False,
                help=_('Enable sending sensor data message via the '
                       'notification bus.')),
    cfg.IntOpt('interval',
               default=600,
               min=1,
               help=_('Seconds between conductor sending sensor data message '
                      'via the notification bus. This was originally for '
                      'consumption via ceilometer, but the data may also '
                      'be consumed via a plugin like '
                      'ironic-prometheus-exporter or any other message bus '
                      'data collector.')),
    cfg.IntOpt('workers',
               default=4, min=1,
               help=_('The maximum number of workers that can be started '
                      'simultaneously for send data from sensors periodic '
                      'task.')),
    cfg.IntOpt('wait_timeout',
               default=300,
               help=_('The time in seconds to wait for send sensors data '
                      'periodic task to be finished before allowing periodic '
                      'call to happen again. Should be less than '
                      'send_sensor_data_interval value.')),
    cfg.ListOpt('data_types',
                default=['ALL'],
                help=_('List of comma separated meter types which need to be '
                       'sent to Ceilometer. The default value, "ALL", is a '
                       'special value meaning send all the sensor data. '
                       'This setting only applies to baremetal sensor data '
                       'being processed through the conductor.')),
    cfg.BoolOpt('enable_for_undeployed_nodes',
                default=False,
                help=_('The default for sensor data collection is to only '
                       'collect data for machines that are deployed, however '
                       'operators may desire to know if there are failures '
                       'in hardware that is not presently in use. '
                       'When set to true, the conductor will collect sensor '
                       'information from all nodes when sensor data '
                       'collection is enabled via the send_sensor_data '
                       'setting.')),
    cfg.BoolOpt('enable_for_conductor',
                default=True,
                help=_('If to include sensor metric data for the Conductor '
                       'process itself in the message payload for sensor '
                       'data which allows operators to gather instance '
                       'counts of actions and states to better manage '
                       'the deployment.')),
    cfg.BoolOpt('enable_for_nodes',
                default=True,
                help=_('If to transmit any sensor data for any nodes under '
                       'this conductor\'s management. This option supersedes '
                       'the ``send_sensor_data_for_undeployed_nodes`` '
                       'setting.')),
    cfg.BoolOpt('enable_async_collection',
                default=False,
                mutable=True,
                help=_('Enable asynchronous sensor data collection using '
                       'AsyncIO. When enabled, drivers that support async '
                       'collection (currently Redfish only) will collect '
                       'sensor data in parallel using an event loop instead '
                       'of worker threads. This can dramatically improve '
                       'collection times for large numbers of nodes.')),
    cfg.IntOpt('async_workers',
               default=200, min=1,
               mutable=True,
               help=_('The maximum number of nodes to collect sensor data '
                      'from at the same time when '
                      '``enable_async_collection`` is enabled. Each node '
                      'being collected from holds a connection to its BMC '
                      'open, so without a bound a conductor managing a '
                      'large number of nodes would open one per node at '
                      'once, and may exhaust file descriptors or overwhelm '
                      'the BMCs. This is the asynchronous counterpart to '
                      'the ``workers`` option, and can be set far higher '
                      'than it, as the collections share a single thread '
                      'rather than taking one each.')),
]


def register_opts(conf):
    conf.register_opts(opts, group='sensor_data')
