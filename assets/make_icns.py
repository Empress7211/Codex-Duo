"""Pack the PNG iconset into an ICNS file on systems where iconutil rejects it."""

from pathlib import Path
import struct

root = Path(__file__).parent
images = {
    b"ic07": "icon_128x128.png",
    b"ic08": "icon_256x256.png",
    b"ic09": "icon_512x512.png",
    b"ic10": "icon_512x512@2x.png",
    b"ic11": "icon_16x16@2x.png",
    b"ic12": "icon_32x32@2x.png",
    b"ic13": "icon_128x128@2x.png",
    b"ic14": "icon_256x256@2x.png",
}
chunks = [(kind, (root / "Orbit.iconset" / filename).read_bytes()) for kind, filename in images.items()]
toc = b"TOC " + struct.pack(">I", 8 + 8 * len(chunks))
toc += b"".join(kind + struct.pack(">I", len(data) + 8) for kind, data in chunks)
payload = toc + b"".join(kind + struct.pack(">I", len(data) + 8) + data for kind, data in chunks)
(root / "Orbit.icns").write_bytes(b"icns" + struct.pack(">I", len(payload) + 8) + payload)
