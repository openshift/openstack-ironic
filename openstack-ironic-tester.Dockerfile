FROM registry.ci.openshift.org/ocp/4.20:base-rhel9

ENV TOX_CONSTRAINTS_FILE="https://releases.openstack.org/constraints/upper/2025.1"

RUN dnf install -y python3-devel python3-pip libpq-devel glibc-langpack-en \
 && dnf clean all \
 && rm -rf /var/cache/yum \
 && python3 -m pip install tox

