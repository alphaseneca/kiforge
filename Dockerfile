ARG KICAD_VERSION=10.0
FROM kicad/kicad:${KICAD_VERSION}

USER root

ARG KICAD_VERSION=10.0
ENV KICAD_VERSION=${KICAD_VERSION}
ENV KICAD10_3DMODEL_DIR=/usr/share/kicad/3dmodels
ENV KISYS3DMOD=/usr/share/kicad/3dmodels

# Install python3-pip and InteractiveHtmlBom at build-time
COPY kiforge.py /tmp/kiforge.py
# librsvg2-bin provides rsvg-convert, the headless SVG->PDF converter KiForge
# falls back to for the homebrew etching PDF (no wx display needed).
# InteractiveHtmlBom is pinned (not "latest") so a breaking or compromised
# upstream release can't silently change or break every CD run -- single source
# of truth is INTERACTIVE_HTML_BOM_PINNED_VERSION in kiforge.py.
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3-pip librsvg2-bin && \
    rm -rf /var/lib/apt/lists/* && \
    IBOM_VER=$(python3 -c "import sys; sys.path.insert(0, '/tmp'); import kiforge; print(kiforge.INTERACTIVE_HTML_BOM_PINNED_VERSION)") && \
    python3 -m pip install --break-system-packages "InteractiveHtmlBom==${IBOM_VER}" && \
    rm -f /tmp/kiforge.py

# Package KiForge exporter scripts into the image
RUN mkdir -p /action
COPY kiforge.py /action/kiforge.py
COPY kiforge.sh /action/kiforge.sh
COPY templates/ /action/templates/
RUN chmod +x /action/kiforge.sh
