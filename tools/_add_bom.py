import pathlib
p = pathlib.Path("tools/serve.ps1")
text = p.read_text(encoding="utf-8")
if text.startswith("\ufeff"):
    text = text.lstrip("\ufeff")
p.write_text(text, encoding="utf-8-sig")
raw = p.read_bytes()[:3]
print("BOM:", raw.hex(), "(efbbbf 即为正确)")
print("文件大小:", len(p.read_bytes()), "bytes")
