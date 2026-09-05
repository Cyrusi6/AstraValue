"""Isolated local OCR process. No network or snapshot/database access."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR
    import rapidocr_onnxruntime

    config = json.loads(args.config.read_text("utf-8"))
    if importlib.metadata.version(config["engine"]) != config["engine_version"]:
        raise ValueError("ocr_engine_version_mismatch")
    package = Path(rapidocr_onnxruntime.__file__).parent
    models = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((package / "models").glob("*.onnx"))}
    if len(models) != 3:
        raise ValueError("local_ocr_models_missing")
    provenance = {
        "config": config, "models": models,
        "worker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "default_config_sha256": hashlib.sha256((package / "config.yaml").read_bytes()).hexdigest(),
        "packages": {name: importlib.metadata.version(name) for name in (
            "rapidocr_onnxruntime", "onnxruntime", "opencv-python", "numpy",
            "Pillow", "PyYAML", "Shapely", "pyclipper",
        )},
    }
    engine = RapidOCR(**config["options"])
    print(json.dumps({"ready": True, "provenance": provenance}), flush=True)
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
            if request.get("operation") == "close":
                return
            image_bytes = Path(request["png_path"]).read_bytes()
            if hashlib.sha256(image_bytes).hexdigest() != request["png_sha256"]:
                raise ValueError("ocr_render_hash_mismatch")
            import io
            im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            width, height = im.size
            passes, selected = [], []
            # Each half owns centers on its side of the midline. The overlap
            # lets detection see whole lines at that boundary without double
            # counting them or guessing that similar text is a duplicate.
            for index, (top, bottom) in enumerate(((0, round(height * .6)), (round(height * .4), height))):
                result, _ = engine(np.array(im.crop((0, top, width, bottom)))[:, :, ::-1])
                lines = []
                for polygon, text, confidence in result or []:
                    box = [[float(x), float(y + top)] for x, y in polygon]
                    center = sum(p[1] for p in box) / 4
                    owned = center < height / 2 if index == 0 else center >= height / 2
                    line = {"box_px": box, "text": text, "confidence": float(confidence),
                            "tile": index, "owned": owned}
                    lines.append(line)
                    if owned:
                        selected.append(dict(line))
                passes.append({"crop_px": [0, top, width, bottom], "lines": lines})
            # A detected line can have an empty recognition result despite
            # clear pixels. One bounded recognition-only pass avoids silently
            # dropping it; keep both the first result and the recovery trace.
            for line in selected:
                if line["text"].strip():
                    continue
                polygon = line["box_px"]
                crop_box = [max(0, math.floor(min(p[0] for p in polygon)) - 4),
                            max(0, math.floor(min(p[1] for p in polygon)) - 4),
                            min(width, math.ceil(max(p[0] for p in polygon)) + 4),
                            min(height, math.ceil(max(p[1] for p in polygon)) + 4)]
                recovered, _ = engine(np.array(im.crop(crop_box))[:, :, ::-1], use_det=False, use_cls=False)
                line["recovery"] = {"method": "recognize-detected-region-once", "crop_px": crop_box,
                                    "original_text": line["text"], "original_confidence": line["confidence"],
                                    "result": recovered}
                if recovered and recovered[0][0].strip():
                    line["text"], line["confidence"] = recovered[0][0], float(recovered[0][1])
            print(json.dumps({"width": width, "height": height, "lines": selected,
                              "passes": passes}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:500]}), flush=True)


if __name__ == "__main__":
    main()
