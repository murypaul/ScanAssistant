"""XMP provenance sidecar for the ingested RAW.

Independent of exiftool: a plain XMP/RDF text template written directly.
Written alongside ingestion if `options.raw_xmp_sidecar` (default true).
Minimal content: `dc:identifier` (inventory name), `dc:source` (original
camera file name), `dc:creator` (operator).
"""

from __future__ import annotations

from xml.sax.saxutils import escape

_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:identifier>{identifier}</dc:identifier>
      <dc:source>{source}</dc:source>
      <dc:creator>
        <rdf:Bag>
          <rdf:li>{creator}</rdf:li>
        </rdf:Bag>
      </dc:creator>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def render_raw_sidecar(*, identifier: str, source: str, creator: str) -> str:
    """Builds the minimal XMP/RDF content of a RAW sidecar."""
    return _TEMPLATE.format(
        identifier=escape(identifier), source=escape(source), creator=escape(creator)
    )
