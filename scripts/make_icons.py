"""Generate the project's original geometric app icon in platform formats."""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parent.parent
out = root / "assets"
out.mkdir(exist_ok=True)
im = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((24, 24, 1000, 1000), radius=236, fill="#EF6B51")
d.rounded_rectangle((220, 240, 804, 708), radius=145, fill="#FFF8F0")
d.polygon([(310, 656), (310, 816), (476, 692)], fill="#FFF8F0")
d.polygon([(534, 316), (422, 504), (514, 504), (470, 634), (652, 426), (548, 426)], fill="#EF6B51")
im.save(out / "icon.png")
im.save(out / "icon.ico", sizes=[(16,16), (24,24), (32,32), (48,48), (64,64), (128,128), (256,256)])
im.save(out / "icon.icns")
print("Generated assets/icon.png, icon.ico and icon.icns")
