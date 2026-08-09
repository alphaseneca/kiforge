FROM kicad/kicad:10.0

USER root

ENV KICAD10_3DMODEL_DIR=/usr/share/kicad/3dmodels
ENV KISYS3DMOD=/usr/share/kicad/3dmodels

# Install python3-pip and InteractiveHtmlBom at build-time
COPY kiforge.py /tmp/kiforge.py
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3-pip && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --break-system-packages InteractiveHtmlBom

# Package KiForge exporter scripts into the image
RUN mkdir -p /action
COPY kiforge.py /action/kiforge.py
COPY kiforge.sh /action/kiforge.sh
COPY templates/ /action/templates/
RUN chmod +x /action/kiforge.sh
