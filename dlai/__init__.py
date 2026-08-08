"""Layer 3 — DLAI: agents, attack path, decision.

Consumes Layer 2 output ONLY. No module in this package may import
from world/ — tests/test_layer_separation.py enforces it. Layer 1 is
the ground truth the blind test scores this layer against; seeing it
would make the blind test meaningless.

The floor plan used here (dlai.floorplan) is read independently from
the published GeoJSON artifact — deployment knowledge, like a site
survey — not from the live world modules.
"""
