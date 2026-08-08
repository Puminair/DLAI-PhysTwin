"""Cisco/Meraki integration tooling for the Centro digital twin.

Layer: none — deployment tooling, outside the three-layer model.
`meraki_provision` builds/pushes the Dashboard API v1 request plan for
the 150 CW9172I logical positions; `scanning_receiver` is the Scanning
API v3 webhook endpoint that feeds `dlai.ingest.normalise_batch`.
Nothing in this package imports `world/` or `sensing/`.
"""
