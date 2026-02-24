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
    name='cifs',
    title='CIFS/SMB Virtual Media Options',
    help=_('Configuration for CIFS/SMB-based virtual media transport. '
           'Operators are responsible for providing a CIFS/SMB server '
           'and ensuring the share specified in base_url is mounted at '
           'share_path on all conductor hosts. The conductor copies '
           'images to share_path, and the BMC accesses them directly '
           'via CIFS/SMB using the base_url. Optional credentials can '
           'be configured and will be passed to the BMC.'))

opts = [
    cfg.StrOpt('base_url',
               help=_('Base URL for CIFS/SMB shares '
                      '(e.g., cifs://server/share or smb://server/share). '
                      'Required for CIFS virtual media support. Must be used '
                      'together with share_path.')),
    cfg.StrOpt('share_path',
               help=_('Local mount point of the CIFS share where images will '
                      'be placed. Required for CIFS virtual media support. '
                      'Must be used together with base_url.')),
    cfg.StrOpt('username',
               help=_('Username for CIFS authentication. If set, credentials '
                      'will be passed to the BMC when inserting virtual '
                      'media.')),
    cfg.StrOpt('password',
               secret=True,
               help=_('Password for CIFS authentication. If set, credentials '
                      'will be passed to the BMC when inserting virtual '
                      'media.')),
]


def register_opts(conf):
    conf.register_group(group)
    conf.register_opts(opts, group='cifs')
