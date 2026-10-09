"""OmniParser YOLOv9 icon detector through OpenVINO (vision fallback only).

Needs a clone of https://github.com/microsoft/OmniParser at OMNIPARSER_DIR
(default ~/OmniParser). On first use the TorchScript weights are downloaded
from Hugging Face (microsoft/OmniParser-v2.0) and converted to OpenVINO IR.
Runs on an Intel GPU when present, otherwise on the CPU.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import openvino as ov
import torch

OMNIPARSER_DIR = Path(os.environ.get("OMNIPARSER_DIR") or Path.home() / "OmniParser")
sys.path.insert(0, str(OMNIPARSER_DIR))

from util.yolov9 import YOLOv9Detector  # noqa: E402

XML = OMNIPARSER_DIR / "weights" / "icon_detect_v3.xml"


class _CompiledModel:
    def __init__(self, compiled):
        self.compiled = compiled

    def __call__(self, image_tensor: torch.Tensor):
        result = self.compiled([image_tensor.detach().numpy()])
        return tuple(torch.from_numpy(np.array(result[key])) for key in result)


def load_detector() -> YOLOv9Detector:
    detector = YOLOv9Detector()
    if not XML.exists():
        XML.parent.mkdir(parents=True, exist_ok=True)
        converted = ov.convert_model(detector.model, example_input=torch.zeros(1, 3, 640, 640))
        ov.save_model(converted, XML)
    core = ov.Core()
    device = "GPU" if "GPU" in core.available_devices else "CPU"
    detector.model = _CompiledModel(core.compile_model(XML, device))
    return detector


def predict(detector: YOLOv9Detector, image, imgsz: int = 640, conf: float = 0.05, max_det: int = 300):
    # predict() wraps the forward in torch.jit.optimized_execution, which
    # rejects a plain callable. Run the same steps without that wrapper.
    image = detector._load_image(image)
    tensor, scale, pad_left, pad_top = detector._preprocess(image, imgsz)
    t0 = time.perf_counter()
    class_scores, boxes = detector._decode(detector.model(tensor))
    infer_ms = (time.perf_counter() - t0) * 1000
    scores, class_ids = class_scores[0].max(dim=-1)
    valid = scores > conf
    scores = scores[valid]
    class_ids = class_ids[valid]
    boxes = boxes[0][valid]
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad_left) / scale
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad_top) / scale
    from torchvision.ops import batched_nms

    keep = batched_nms(boxes, scores, class_ids, 0.7)[:max_det]
    boxes = boxes[keep]
    scores = scores[keep]
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clamp(0, image.width)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clamp(0, image.height)
    return boxes, scores, round(infer_ms, 1)
