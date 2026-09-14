.. meta::
   :description: Complete installation guide for OpenStack Ironic bare metal service. Step-by-step setup, integration with OpenStack services, and deployment configurations.
   :keywords: ironic installation, openstack installation, bare metal setup, ironic configuration, driver setup, service deployment
   :author: OpenStack Ironic Team
   :robots: index, follow
   :audience: system administrators, cloud operators

====================================
Installing Ironic Bare Metal Service
====================================

The Bare Metal service is a collection of components that provides support to
manage and provision physical machines. Ironic can run as a standalone service
or as part of an OpenStack deployment. Sections covering integration with other
OpenStack services assume a working setup of OpenStack following the
`OpenStack Installation Guides <https://docs.openstack.org/latest/install>`_.

This chapter contains the following sections:

.. toctree::
   :maxdepth: 2

   deployment-scenarios.rst
   get_started.rst
   refarch/index
   install.rst
   deploy-ramdisk.rst
   configure-integration.rst
   setup-drivers.rst
   enrollment.rst
   standalone.rst
   configdrive.rst
   graphical-console.rst
   advanced.rst
   troubleshooting.rst
   next-steps.rst

.. toctree::
   :hidden:

   creating-images.rst
