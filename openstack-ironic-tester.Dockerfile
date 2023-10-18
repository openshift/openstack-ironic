FROM registry.ci.openshift.org/ocp/builder:rhel-9-base-openshift-4.15

RUN dnf install -y python3-devel python3-pip libpq-devel glibc-langpack-en \
 && dnf clean all \
 && rm -rf /var/cache/yum \
 && python3 -m pip install tox

