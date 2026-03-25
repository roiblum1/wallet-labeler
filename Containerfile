FROM registry.redhat.io/openshift4/ose-cli:latest

USER 0

# Install Python3 + PyYAML (ssh client is already in ose-cli)
RUN dnf install -y python3 python3-pyyaml openssh-clients && \
    dnf clean all

COPY wallet_labeler.py /app/wallet_labeler.py

USER 1001

ENTRYPOINT ["python3", "/app/wallet_labeler.py"]
CMD ["-c", "/config/config.yaml"]
