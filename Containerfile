FROM registry.redhat.io/openshift4/ose-cli:latest

USER 0

# Install Python3 + PyYAML (ssh client is already in ose-cli)
RUN dnf install -y python3 python3-pyyaml openssh-clients && \
    dnf clean all

COPY wallet_labeler/ /app/wallet_labeler/

USER 1001

ENTRYPOINT ["python3", "-m", "wallet_labeler"]
CMD ["-c", "/config/config.yaml"]
