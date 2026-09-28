"""The vision edge node — runs next to the cameras, on the mine LAN.

``runtime``, ``outbox`` and ``publish`` use only the standard library + the pure
``minemonitor.vision`` modules, so the full edge pipeline runs in CI with the simulated model.
Real frame sources and model adapters (OpenCV, ONNX Runtime, NumPy) live in modules that import
those libraries lazily and need the optional ``vision`` extra. **The core never imports this
package** — it is a separate, independently failing process (``docs/VISION_ARCHITECTURE.md`` §9).
"""
