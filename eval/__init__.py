"""Evaluation — the only place ground truth and inference legally meet.

eval/ may import world/, sensing/ AND dlai/, because comparing Layer 3
output against Layer 1 truth is its entire purpose. Nothing here feeds
back into dlai/.
"""
