# Local Android debugging only: the Bragi server on a fake PipeWire, plus the
# Roc 0.4 CLI tools that stand in for sagepi's PipeWire Roc modules.
# Trixie matches sagepi (roc-toolkit-tools 0.4.0+dfsg-5).
FROM python:3.13-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends roc-toolkit-tools \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /src
