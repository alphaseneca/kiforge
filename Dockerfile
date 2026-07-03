FROM kicad/kicad:10.0

USER root

# Install python3-pip, InteractiveHtmlBom, and Fabrication Toolkit at build-time
COPY kiforge.py /tmp/kiforge.py
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3-pip && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --break-system-packages InteractiveHtmlBom && \
    python3 -c "import sys; sys.path.insert(0, '/tmp'); import kiforge; kiforge.install_fabrication_toolkit_from_release()"

# Package KiForge exporter scripts into the image
RUN mkdir -p /action
COPY kiforge.py /action/kiforge.py
COPY kiforge.sh /action/kiforge.sh
COPY templates/ /action/templates/
RUN chmod +x /action/kiforge.sh
