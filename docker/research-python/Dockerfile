FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk=1:20220127+repack1-1 \
    && rm -rf /var/lib/apt/lists/*
COPY docker/research-python/requirements.txt /opt/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /opt/requirements.txt
RUN mkdir -p /usr/local/share/fonts \
    && python -c "from fontTools.ttLib import TTCollection; TTCollection('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc').fonts[2].save('/usr/local/share/fonts/NotoSansCJK-SC.otf')"
RUN mkdir -p /input /output /control /home/sandbox \
    && chown 65532:65532 /output /control /home/sandbox
COPY src/analysis/research/python_sandbox_worker.py /opt/research-python/worker.py
COPY src/analysis/research/python_sandbox_exporter.py /opt/research-python/exporter.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 \
    MPLCONFIGDIR=/tmp/matplotlib XDG_CACHE_HOME=/tmp/cache HOME=/home/sandbox \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
USER 65532:65532
WORKDIR /output
ENTRYPOINT ["python", "-I", "/opt/research-python/worker.py"]
