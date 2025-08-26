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

group = cfg.OptGroup(
    name='nfs',
    title='NFS Virtual Media Options',
    help=_('Configuration for NFS-based virtual media transport. '
           'Operators are responsible for providing an NFSv3 server '
           'and ensuring the NFS export specified in base_url is '
           'mounted at share_path on all conductor hosts. The conductor '
           'copies images to share_path, and the BMC accesses them '
           'directly via NFSv3 using the base_url. No credentials are '
           'sent to the BMC for NFS.'))

opts = [
    cfg.StrOpt('base_url',
               help=_('Base URL for NFS shares (e.g., nfs://server/export). '
                      'Required for NFS virtual media support. Must be used '
                      'together with share_path.')),
    cfg.StrOpt('share_path',
               help=_('Local mount point of the NFS share where images will '
                      'be placed. Required for NFS virtual media support. '
                      'Must be used together with base_url.')),
]


def register_opts(conf):
    conf.register_group(group)
    conf.register_opts(opts, group='nfs')
