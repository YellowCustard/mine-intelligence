"""Mine Monitor Vision — first-party perception (Stage B, ``docs/VISION_BUILD_GATE.md``).

Everything directly under this package is **pure Python with no computer-vision dependency**, so
the core can import it (ingest, promotion, recompute) and CI stays weights-free and GPU-free.
The edge runtime and model adapters that need NumPy/OpenCV/ONNX Runtime live in
``minemonitor.vision.edge`` behind the optional ``vision`` extra; the core never imports it.

The line this package enforces: the model answers *"what is in the frame?"*; versioned site rules
answer *"what does that mean here?"* Perception emits ``person``, never *who*. Every output is
advisory.
"""
