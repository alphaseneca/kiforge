FROM kicad/kicad:10.0

USER root

ENV KICAD10_3DMODEL_DIR=/usr/share/kicad/3dmodels
ENV KISYS3DMOD=/usr/share/kicad/3dmodels

# Install python3-pip and InteractiveHtmlBom at build-time
COPY kiforge.py /tmp/kiforge.py
# librsvg2-bin provides rsvg-convert, the headless SVG->PDF converter KiForge
# falls back to for the homebrew etching PDF (no Qt/wx display needed).
# InteractiveHtmlBom is pinned (not "latest") so a breaking or compromised
# upstream release can't silently change or break every CD run -- keep this
# version in sync with INTERACTIVE_HTML_BOM_PINNED_VERSION in kiforge.py.
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3-pip librsvg2-bin && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --break-system-packages InteractiveHtmlBom==2.11.2

# Package KiForge exporter scripts into the image
RUN mkdir -p /action
COPY kiforge.py /action/kiforge.py
COPY kiforge.sh /action/kiforge.sh
COPY templates/ /action/templates/
RUN chmod +x /action/kiforge.sh
