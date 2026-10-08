FROM registry.ci.openshift.org/ocp/5.1:base-rhel10

RUN dnf install -y python3.14-devel python3.14-pip libpq-devel glibc-langpack-en git \
 && dnf clean all \
 && rm -rf /var/cache/yum \
 && python3.14 -m pip install tox

