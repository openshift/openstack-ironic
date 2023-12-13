FROM registry.ci.openshift.org/ocp/builder:rhel-9-base-openshift-4.15

RUN curl http://base-4-16-rhel-9-ironic-prevalidation.ocp.svc > /etc/yum.repos.d/base-4-16-rhel-9-ironic-prevalidation.repo \
 && dnf install -y python3-pbr python3-devel python3-pip libpq-devel glibc-langpack-en \
 && dnf clean all \
 && rm -rf /var/cache/yum \
 && python3 -m pip install tox

